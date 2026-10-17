"""Fetch market RSS feeds and push new headlines to WeChat via ServerChan."""

from __future__ import annotations

import argparse
import email.utils
import html
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "market_monitor.json"
STATE_PATH = ROOT / "market_monitor.sqlite3"
USER_AGENT = "PersonalMarketMonitor/1.0 (RSS reader)"
FEEDS = [
    {"name": "CNBC Finance", "url": "https://www.cnbc.com/id/10000664/device/rss/rss.html", "category": "美股与金融"},
    {"name": "CoinDesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/", "category": "加密资产"},
    {"name": "Federal Reserve", "url": "https://www.federalreserve.gov/feeds/press_all.xml", "category": "美联储"},
    {"name": "SEC Press Releases", "url": "https://www.sec.gov/news/pressreleases.rss", "category": "监管公告"},
]
ATOM = "http://www.w3.org/2005/Atom"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {"feeds": FEEDS, "max_items": 12, "lookback_hours": 36}
    with CONFIG_PATH.open("r", encoding="utf-8") as stream:
        config = json.load(stream)
    config.setdefault("feeds", FEEDS)
    config.setdefault("max_items", 12)
    config.setdefault("lookback_hours", 36)
    return config


def clean_text(value: str | None) -> str:
    value = re.sub(r"<[^>]*>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def fetch_feed(feed: dict) -> list[dict]:
    request = urllib.request.Request(feed["url"], headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        root = ET.fromstring(response.read())

    entries = root.findall(".//item")
    is_atom = not entries
    if is_atom:
        entries = root.findall(f".//{{{ATOM}}}entry")

    items = []
    for entry in entries:
        if is_atom:
            title = entry.findtext(f"{{{ATOM}}}title", default="")
            link_node = entry.find(f"{{{ATOM}}}link")
            link = link_node.attrib.get("href", "") if link_node is not None else ""
            published = entry.findtext(f"{{{ATOM}}}published") or entry.findtext(f"{{{ATOM}}}updated")
            description = entry.findtext(f"{{{ATOM}}}summary") or ""
        else:
            title = entry.findtext("title", default="")
            link = entry.findtext("link", default="")
            published = entry.findtext("pubDate") or entry.findtext("{http://purl.org/dc/elements/1.1/}date")
            description = entry.findtext("description") or ""
        title = clean_text(title)
        link = (link or "").strip()
        if not title or not link:
            continue
        items.append({
            "title": title,
            "link": link,
            "published": parse_date(published),
            "description": clean_text(description),
            "source": feed.get("name", "RSS"),
            "category": feed.get("category", "市场资讯"),
        })
    return items


def init_state(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE TABLE IF NOT EXISTS sent_items (url TEXT PRIMARY KEY, sent_at TEXT NOT NULL)")
    connection.commit()


def select_new_items(connection: sqlite3.Connection, items: list[dict], lookback_hours: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    fresh = []
    for item in items:
        published = item["published"]
        if published and published < cutoff:
            continue
        if connection.execute("SELECT 1 FROM sent_items WHERE url = ?", (item["link"],)).fetchone():
            continue
        fresh.append(item)
    fresh.sort(key=lambda item: item["published"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return fresh


def push_serverchan(sendkey: str, title: str, body: str) -> None:
    url = f"https://sctapi.ftqq.com/{urllib.parse.quote(sendkey, safe='')}.send"
    data = urllib.parse.urlencode({"title": title, "desp": body}).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.loads(response.read().decode("utf-8"))
    if result.get("code") != 0:
        raise RuntimeError(f"Server酱推送失败：{result.get('message', result)}")


def format_digest(items: list[dict]) -> str:
    lines = [f"本次发现 {len(items)} 条新消息：", ""]
    for item in items:
        when = item["published"].astimezone().strftime("%m-%d %H:%M") if item["published"] else "时间未知"
        lines.extend([
            f"【{item['category']}】{item['title']}",
            f"来源：{item['source']} | {when}",
            item["link"],
            "",
        ])
    lines.append("提示：此为 RSS 标题摘要，重要信息请打开原文核实。")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="监控全球市场 RSS，并通过微信推送新消息")
    parser.add_argument("--test", action="store_true", help="发送一条测试消息")
    args = parser.parse_args()

    sendkey = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
    if not sendkey:
        print("未设置 SERVERCHAN_SENDKEY。请先按 README_CN.md 配置 Server酱 SendKey。", file=sys.stderr)
        return 2

    if args.test:
        try:
            push_serverchan(sendkey, "市场监控测试", "测试成功。下一步运行 market_monitor.py 即可获取市场快讯。")
        except (OSError, ValueError, RuntimeError) as error:
            print(f"测试推送失败：{error}", file=sys.stderr)
            return 1
        print("测试消息已发送，请检查微信。")
        return 0

    config = load_config()
    all_items = []
    failures = []
    for feed in config["feeds"]:
        try:
            all_items.extend(fetch_feed(feed))
        except (OSError, urllib.error.URLError, ET.ParseError, KeyError, TimeoutError) as error:
            failures.append(f"{feed.get('name', feed.get('url'))}: {error}")

    with sqlite3.connect(STATE_PATH) as connection:
        init_state(connection)
        fresh = select_new_items(connection, all_items, int(config["lookback_hours"]))
        fresh = fresh[:max(1, int(config["max_items"]))]
        if not fresh:
            print(f"没有新消息。成功读取 {len(config['feeds']) - len(failures)}/{len(config['feeds'])} 个来源。")
            if failures:
                print("来源异常：" + "；".join(failures), file=sys.stderr)
            return 0
        try:
            push_serverchan(sendkey, f"全球市场快讯：{len(fresh)} 条", format_digest(fresh))
        except (OSError, ValueError, RuntimeError) as error:
            print(f"推送失败，消息未标记为已发送：{error}", file=sys.stderr)
            return 1
        sent_at = datetime.now(timezone.utc).isoformat()
        connection.executemany("INSERT OR IGNORE INTO sent_items(url, sent_at) VALUES (?, ?)", [(item["link"], sent_at) for item in fresh])
        connection.commit()

    print(f"已推送 {len(fresh)} 条新消息。")
    if failures:
        print("部分来源异常：" + "；".join(failures), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
