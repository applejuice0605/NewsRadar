from __future__ import annotations

import copy
import importlib.util
import re
import unittest
from datetime import datetime, timezone
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "generate_site_insights.py"
SPEC = importlib.util.spec_from_file_location("status_display", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class StatusDisplayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.health = MODULE.build_public_health(
            {}, now=datetime(2026, 10, 29, 16, 1, 2, tzinfo=timezone.utc)
        )
        self.pilot = MODULE.build_public_pilot({})

    def test_all_status_timestamps_use_shanghai_time(self) -> None:
        fields = [
            (self.health, "generated_at"),
            (self.health["collection"], "last_started_at"),
            (self.health["collection"], "last_completed_at"),
            (self.health["notification"], "last_attempt_at"),
            (self.health["notification"], "last_success_at"),
            (self.health["publish"], "last_attempt_at"),
            (self.health["publish"], "last_success_at"),
            (self.health["schedule"], "next_expected_at"),
            (self.health["podcast"], "generated_at"),
            (self.pilot, "generated_at"),
        ]
        timestamps = (
            "2026-10-29T16:01:02.123Z",
            "2026-10-30T00:01:02.123+08:00",
            "2026-10-29T11:01:02.123-05:00",
            "2026-10-29T21:31:02.123+05:30",
        )
        for index, (mapping, key) in enumerate(fields):
            mapping[key] = timestamps[index % len(timestamps)]
        self.health["podcast"]["date"] = "2026-10-29"
        before = copy.deepcopy((self.health, self.pilot))

        page = MODULE.render_status_html(self.health, self.pilot)

        self.assertIn("北京时间（Asia/Shanghai）", page)
        self.assertEqual(page.count("2026-10-30 00:01:02"), len(fields))
        self.assertNotRegex(page, r"\d{4}-\d{2}-\d{2}T\d{2}:")
        self.assertIn("<dt>最新一期</dt><dd>2026-10-29</dd>", page)
        self.assertEqual((self.health, self.pilot), before)

    def test_missing_and_invalid_times_display_unknown(self) -> None:
        for value in (None, "", "not-a-time", 123):
            with self.subTest(value=value):
                self.health["generated_at"] = value
                self.health["podcast"]["generated_at"] = value
                self.pilot["generated_at"] = value
                page = MODULE.render_status_html(self.health, self.pilot)
                self.assertIn("生成于 未知", page)
                for label in ("最近开始", "最近完成", "最近尝试", "最近成功", "生成时间", "下次预期", "最近探测"):
                    self.assertIn(f"<dt>{label}</dt><dd>未知</dd>", page)

        del self.health["generated_at"]
        del self.health["podcast"]["generated_at"]
        del self.pilot["generated_at"]
        for section, keys in (
            ("collection", ("last_started_at", "last_completed_at")),
            ("notification", ("last_attempt_at", "last_success_at")),
            ("publish", ("last_attempt_at", "last_success_at")),
            ("schedule", ("next_expected_at",)),
        ):
            for key in keys:
                del self.health[section][key]
        self.assertIn("生成于 未知", MODULE.render_status_html(self.health, self.pilot))

    def test_naive_time_follows_existing_utc_convention(self) -> None:
        self.health["podcast"]["generated_at"] = "2026-10-29T16:01:02"
        page = MODULE.render_status_html(self.health, self.pilot)
        self.assertIn("<dt>生成时间</dt><dd>2026-10-30 00:01:02</dd>", page)

    def test_duration_and_minute_intervals_are_display_only_integers(self) -> None:
        for duration, gap, interval, expected in (
            (12.8, 60.2, 30.8, ("13 秒", "60 分钟", "每 31 分钟")),
            (0.0, 0.0, 60.0, ("0 秒", "0 分钟", "每 60 分钟")),
            (None, None, None, ("未知", "未知", "每 未知 分钟")),
        ):
            with self.subTest(duration=duration, gap=gap, interval=interval):
                self.health["collection"].update(duration_seconds=duration, gap_minutes=gap)
                self.health["schedule"]["interval_minutes"] = interval
                before = copy.deepcopy(self.health)
                page = MODULE.render_status_html(self.health, self.pilot)
                self.assertIn(f"<dt>耗时</dt><dd>{expected[0]}</dd>", page)
                self.assertIn(f"<dt>间隔</dt><dd>{expected[1]}</dd>", page)
                self.assertIn(expected[2], page)
                self.assertIsNone(re.search(r"\d+\.\d+ (?:秒|分钟)", page))
                self.assertEqual(self.health, before)


if __name__ == "__main__":
    unittest.main()
