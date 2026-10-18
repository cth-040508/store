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
from difflib import SequenceMatcher
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
        return {"feeds": FEEDS, "max_items": 5, "lookback_hours": 36}
    with CONFIG_PATH.open("r", encoding="utf-8") as stream:
        config = json.load(stream)
    config.setdefault("feeds", FEEDS)
    config.setdefault("max_items", 5)
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


def assess_importance(item: dict) -> tuple[int, str]:
    """Conservative headline rules; a source alone never qualifies an article."""
    title = item["title"].lower()
    def has(pattern: str) -> bool:
        return bool(re.search(pattern, title, re.I))

    if has(r"\b(sponsored|paid post|price prediction|technical analysis|opinion|horoscope|best .* to buy|could .* reach|what to watch|preview|week ahead)\b"):
        return 0, "预测、评论或推广"
    if has(r"\b(expects?|predicts?|forecasts?|bets? on|could|may|might|eyes|weighs|considers)\b"):
        return 0, "尚未落地的预测或讨论"

    if has(r"\b(fed|fomc|federal reserve|ecb|bank of japan|bank of england|pboc)\b") and has(r"\b(cuts?|raises?|holds?|keeps?|hikes?|decision|statement|minutes|quantitative|emergency)\b"):
        return 100, "主要央行政策与会议结果"
    if has(r"\b(cpi|pce|nonfarm|non-farm|payrolls|inflation|gdp|unemployment)\b") and has(r"\b(u\.?s\.?|america|payrolls|nonfarm|non-farm)\b") and has(r"\b(rises?|falls?|rose|fell|increases?|decreases?|hits?|slows?|cools?|accelerates?|report|data|released|grew|shrinks?)\b"):
        return 95, "美国关键经济数据"
    if has(r"\b(tariffs?|sanctions?|trade deal|war|invasion|strait of hormuz)\b") and has(r"\b(announces?|imposes?|signs?|launches?|blocks?|closes?|escalates?|strikes?|agrees?|halts?)\b"):
        return 90, "重大贸易或地缘事件"

    crypto = has(r"\b(bitcoin|btc|ethereum|ether|eth|crypto|stablecoin|usdt|usdc|tether|binance|coinbase|kraken|defi|solana)\b")
    if crypto and has(r"\b(hack(?:ed)?|exploit(?:ed)?|breach|stolen|depeg(?:s|ged)?|bankruptcy|bankrupt|insolven\w*|halts? withdrawals|suspends? withdrawals)\b"):
        return 90, "加密安全或偿付风险"
    if crypto and has(r"\b(sec|cftc|congress|senate|court|regulator\w*|etf|law|bill)\b") and has(r"\b(approves?|rejects?|denies|bans?|passes?|signs?|rules?|ruling|sues?|charges?|settles?|settlement)\b"):
        return 85, "加密监管、法律或 ETF 决定"

    if has(r"\b(apple|microsoft|nvidia|amazon|alphabet|google|meta|tesla|broadcom|tsmc|jpmorgan|berkshire)\b") and has(r"\b(earnings|results|guidance|outlook|revenue|profit|acquires?|acquisition|antitrust)\b"):
        return 80, "重点公司业绩或重大经营事件"
    if has(r"\b(bank|banking|treasury|bond market|stock exchange|nyse|nasdaq)\b") and has(r"\b(failure|fails|collapse|bailout|emergency|default|outage|shutdown)\b"):
        return 90, "金融系统或交易中断风险"
    return 0, "未命中重大事件规则"


def title_key(title: str) -> str:
    return " ".join(re.findall(r"\w+", title.lower()))


def filter_important(items: list[dict], recent_titles: list[str]) -> list[dict]:
    ranked = []
    for item in items:
        score, reason = assess_importance(item)
        if score:
            ranked.append({**item, "importance": score, "reason": reason})
    ranked.sort(key=lambda item: (item["importance"], item["published"] or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    keys = [title_key(title) for title in recent_titles]
    selected = []
    urls = set()
    for item in ranked:
        key = title_key(item["title"])
        if item["link"] in urls or any(SequenceMatcher(None, key, other).ratio() >= 0.85 for other in keys):
            continue
        selected.append(item)
        keys.append(key)
        urls.add(item["link"])
    return selected


def init_state(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE TABLE IF NOT EXISTS sent_items (url TEXT PRIMARY KEY, sent_at TEXT NOT NULL)")
    connection.execute("CREATE TABLE IF NOT EXISTS sent_headlines (title TEXT NOT NULL, sent_at TEXT NOT NULL)")
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
            f"入选原因：{item['reason']}",
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
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=36)).isoformat()
        recent_titles = [row[0] for row in connection.execute("SELECT title FROM sent_headlines WHERE sent_at >= ?", (cutoff,))]
        fresh = filter_important(fresh, recent_titles)
        fresh = fresh[:max(1, int(config["max_items"]))]
        if not fresh:
            print(f"没有符合重点筛选规则的新消息。成功读取 {len(config['feeds']) - len(failures)}/{len(config['feeds'])} 个来源。")
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
        connection.executemany("INSERT INTO sent_headlines(title, sent_at) VALUES (?, ?)", [(item["title"], sent_at) for item in fresh])
        connection.execute("DELETE FROM sent_headlines WHERE sent_at < ?", (cutoff,))
        connection.commit()

    print(f"已推送 {len(fresh)} 条新消息。")
    if failures:
        print("部分来源异常：" + "；".join(failures), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
