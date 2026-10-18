from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "generate_site_insights.py"
SPEC = importlib.util.spec_from_file_location("generate_site_insights", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def create_news_db(path: Path, title: str, tag: str | None = None) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            """
            CREATE TABLE platforms (id TEXT PRIMARY KEY, name TEXT);
            CREATE TABLE news_items (id INTEGER PRIMARY KEY, title TEXT, platform_id TEXT, rank INTEGER, url TEXT, mobile_url TEXT);
            CREATE TABLE rank_history (id INTEGER PRIMARY KEY, news_item_id INTEGER, rank INTEGER, crawl_time TEXT);
            CREATE TABLE ai_filter_tags (id INTEGER PRIMARY KEY, tag TEXT, priority INTEGER, status TEXT);
            CREATE TABLE ai_filter_results (news_item_id INTEGER, source_type TEXT, tag_id INTEGER, relevance_score REAL, status TEXT);
            INSERT INTO platforms VALUES ('source', '测试平台');
            """
        )
        connection.execute("INSERT INTO news_items VALUES (1, ?, 'source', 3, 'https://example.test/item', '')", (title,))
        connection.executemany("INSERT INTO rank_history VALUES (?, 1, ?, ?)", [(1, 8, "01:00"), (2, 3, "02:00")])
        if tag:
            connection.execute("INSERT INTO ai_filter_tags VALUES (1, ?, 1, 'active')", (tag,))
            connection.execute("INSERT INTO ai_filter_results VALUES (1, 'hotlist', 1, .9, 'active')")
        connection.commit()


class GenerateSiteInsightsTest(unittest.TestCase):
    def test_generates_safe_outputs_and_keeps_daily_ids_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            news = repo / "TrendRadar" / "output" / "news"
            rss = repo / "TrendRadar" / "output" / "rss"
            meta = repo / "TrendRadar" / "output" / "meta"
            site = repo / "docs"
            news.mkdir(parents=True)
            rss.mkdir(parents=True)
            meta.mkdir(parents=True)
            create_news_db(news / "2026-10-14.db", "前一日文章", "AI")
            create_news_db(news / "2026-10-15.db", "当日文章", "AI")
            with closing(sqlite3.connect(news / "2026-10-15.db")) as connection:
                connection.execute("UPDATE news_items SET url = 'javascript:alert(1)'")
                connection.commit()
            (news / "2026-10-13.db").write_bytes(b"not sqlite")
            (meta / "runtime_health.json").write_text(
                json.dumps(
                    {
                        "collection": {"success": True, "item_count": 2, "last_completed_at": "2026-10-15T01:00:00+00:00"},
                        "notification": {"status": "failed", "reason": "webhook https://secret.test in /Users/private"},
                        "publish": {"status": "success", "commit": "abc1234", "path": "/Users/private"},
                        "environment": {"API_KEY": "secret"},
                    }
                ),
                encoding="utf-8",
            )

            health, trends, skipped = MODULE.generate_site_insights(
                repo, site, now=datetime(2026, 10, 15, 2, tzinfo=timezone.utc)
            )

            self.assertEqual(health["notification"]["reason"], "details_redacted")
            health_text = (site / "health.json").read_text(encoding="utf-8")
            self.assertNotIn("/Users/private", health_text)
            self.assertNotIn("secret.test", health_text)
            self.assertNotIn("API_KEY", health_text)
            self.assertEqual(trends["ranges"]["1"]["topics"][0]["article_count"], 1)
            self.assertEqual(trends["ranges"]["7"]["topics"][0]["article_count"], 2)
            self.assertEqual(trends["ranges"]["7"]["covered_days"], 2)
            self.assertTrue(trends["ranges"]["7"]["partial"])
            self.assertEqual(trends["ranges"]["7"]["topics"][0]["basis"], "ai_filter")
            self.assertEqual(trends["ranges"]["1"]["topics"][0]["top_articles"][0]["url"], "")
            self.assertEqual(len(skipped), 1)
            for filename in ("health.json", "trends.json", "status.html", "trends.html"):
                self.assertTrue((site / filename).is_file())
            self.assertNotIn("https://cdnjs", (site / "trends.html").read_text(encoding="utf-8"))

    def test_falls_back_to_platform_topics_without_ai_matches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            news = repo / "TrendRadar" / "output" / "news"
            news.mkdir(parents=True)
            create_news_db(news / "2026-10-15.db", "普通文章")
            site = repo / "docs"

            _, trends, _ = MODULE.generate_site_insights(
                repo, site, now=datetime(2026, 10, 15, 2, tzinfo=timezone.utc)
            )

            one_day = trends["ranges"]["1"]
            self.assertEqual(one_day["basis"], "platform_fallback")
            self.assertEqual(one_day["topics"][0]["tag"], "测试平台")
            self.assertIsNone(one_day["topics"][0]["average_relevance"])

    def test_marks_overdue_or_failed_sources_as_degraded(self) -> None:
        health = MODULE.build_public_health(
            {
                "collection": {
                    "success": True,
                    "source_failed": 1,
                    "last_completed_at": "2026-10-15T00:00:00+00:00",
                },
                "notification": {"status": "skipped"},
                "publish": {"status": "success"},
                "schedule": {"interval_minutes": 60},
            },
            now=datetime(2026, 10, 15, 2, tzinfo=timezone.utc),
        )

        self.assertEqual(health["overall_status"], "degraded")

    def test_projects_source_pilot_without_endpoint_details(self) -> None:
        pilot = MODULE.build_public_pilot(
            {
                "configured": True,
                "generated_at": "2026-10-15T02:00:00+00:00",
                "endpoint": "https://secret.example/api?token=hidden",
                "sources": [{"source": "hackernews", "success": True, "item_count": 30}],
                "recommendations": {
                    "hackernews": {"status": "observing", "metrics": {"sample_days": 2}}
                },
            }
        )

        self.assertEqual(pilot["status"], "observing")
        self.assertEqual(pilot["sources"][0]["sample_days"], 2)
        self.assertNotIn("endpoint", pilot)


if __name__ == "__main__":
    unittest.main()
