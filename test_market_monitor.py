import sqlite3
import unittest
from datetime import datetime, timezone

from market_monitor import assess_importance, filter_important, init_state


def article(title, link="https://example.com/news"):
    return {"title": title, "link": link, "published": datetime.now(timezone.utc)}


class ImportanceTests(unittest.TestCase):
    def test_important_events(self):
        for title in [
            "Fed holds interest rates steady in policy decision",
            "US CPI inflation rises more than expected",
            "Nvidia earnings beat estimates as revenue surges",
            "SEC approves spot Bitcoin ETF",
            "Binance suspends withdrawals after hack",
            "US imposes new tariffs on China",
            "NYSE outage causes trading shutdown",
        ]:
            with self.subTest(title=title):
                self.assertGreater(assess_importance(article(title))[0], 0)

    def test_noise(self):
        for title in [
            "Bitcoin price prediction: next stop $200,000",
            "Analyst predicts Fed cuts in December",
            "SEC appoints new director",
            "Federal Reserve announces community conference",
            "Crypto prices rise as traders await news",
            "Sponsored: best crypto to buy today",
            "Apple shares edge higher on analyst upgrade",
            "Bitcoin ETF approval could come next week",
        ]:
            with self.subTest(title=title):
                self.assertEqual(assess_importance(article(title))[0], 0)

    def test_ranking_and_duplicate_headlines(self):
        earnings = article("Nvidia earnings beat estimates")
        fed = article("Fed holds interest rates steady", "https://example.com/fed")
        duplicate = article("Fed holds interest rates steady!", "https://other.com/fed")
        selected = filter_important([earnings, fed, duplicate], [])
        self.assertEqual(len(selected), 2)
        self.assertEqual(selected[0]["title"], fed["title"])
        self.assertEqual(filter_important([fed], [fed["title"]]), [])

    def test_existing_database_migration(self):
        with sqlite3.connect(":memory:") as db:
            db.execute("CREATE TABLE sent_items (url TEXT PRIMARY KEY, sent_at TEXT NOT NULL)")
            db.execute("INSERT INTO sent_items VALUES ('old', '2026-01-01')")
            init_state(db)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sent_items").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sent_headlines").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
