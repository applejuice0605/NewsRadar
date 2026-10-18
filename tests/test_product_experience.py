from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "generate_product_experience", REPO / "scripts" / "generate_product_experience.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def create_news_db(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            """
            CREATE TABLE platforms (id TEXT PRIMARY KEY, name TEXT);
            CREATE TABLE news_items (
              id INTEGER PRIMARY KEY, title TEXT, platform_id TEXT, rank INTEGER,
              url TEXT, mobile_url TEXT, first_crawl_time TEXT, last_crawl_time TEXT,
              crawl_count INTEGER
            );
            CREATE TABLE rank_history (id INTEGER PRIMARY KEY, news_item_id INTEGER, rank INTEGER, crawl_time TEXT);
            CREATE TABLE ai_filter_tags (id INTEGER PRIMARY KEY, tag TEXT, priority INTEGER, status TEXT);
            CREATE TABLE ai_filter_results (news_item_id INTEGER, source_type TEXT, tag_id INTEGER, relevance_score REAL, status TEXT);
            INSERT INTO platforms VALUES ('source-a', '来源甲'), ('source-b', '来源乙');
            INSERT INTO ai_filter_tags VALUES (1, '大模型进展', 1, 'active');
            INSERT INTO news_items VALUES
              (1, 'OpenAI 发布新模型', 'source-a', 2, 'https://example.test/story?utm_source=x', '', '2026-10-18T01:00:00+00:00', '2026-10-18T03:00:00+00:00', 5),
              (2, 'OpenAI 新模型引发行业关注', 'source-b', 4, 'https://other.test/story', '', '2026-10-18T02:00:00+00:00', '2026-10-18T03:00:00+00:00', 3);
            INSERT INTO rank_history VALUES (1, 1, 8, '01:00'), (2, 1, 2, '03:00'), (3, 2, 4, '03:00');
            INSERT INTO ai_filter_results VALUES (1, 'hotlist', 1, .95, 'active'), (2, 'hotlist', 1, .88, 'active');
            """
        )


class ProductExperienceTest(unittest.TestCase):
    def test_builds_traceable_events_scores_and_shadow_notifications(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            news = repo / "TrendRadar" / "output" / "news"
            news.mkdir(parents=True)
            create_news_db(news / "2026-10-18.db")

            payload = MODULE.build_experience(
                repo, now=datetime(2026, 10, 18, 4, tzinfo=timezone.utc)
            )

            self.assertEqual(payload["as_of"], "2026-10-18")
            self.assertEqual(len(payload["articles"]), 2)
            self.assertEqual(len(payload["events"]), 1)
            self.assertEqual(payload["events"][0]["source_count"], 2)
            self.assertEqual(len(payload["events"][0]["analysis"]["evidence_ids"]), 2)
            self.assertEqual(payload["articles"][0]["url"], "https://example.test/story")
            self.assertIn("components", payload["articles"][0]["score"])
            self.assertEqual(payload["notification_preview"]["mode"], "shadow_only")
            self.assertFalse(payload["notification_preview"]["changes_delivery"])
            self.assertLessEqual(payload["notification_preview"]["candidate_count"], 3)

    def test_writes_search_event_and_json_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = {
                "articles": [], "events": [], "notification_preview": {"mode": "shadow_only"}
            }
            MODULE.write_outputs(root, payload)
            for filename in (
                "experience.json", "search-index.json", "events.html", "search.html", "notification-preview.json"
            ):
                self.assertTrue((root / filename).is_file())
            self.assertIn("EVENT TRACKER", (root / "events.html").read_text(encoding="utf-8"))
            self.assertEqual(
                json.loads((root / "notification-preview.json").read_text())["mode"],
                "shadow_only",
            )


if __name__ == "__main__":
    unittest.main()
