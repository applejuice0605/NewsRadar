from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "generate_daily_podcast.py"
SPEC = importlib.util.spec_from_file_location("generate_daily_podcast", MODULE_PATH)
assert SPEC and SPEC.loader
podcast = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(podcast)


class DailyPodcastTest(unittest.TestCase):
    def test_extracts_only_ai_section(self) -> None:
        parser = podcast.AISectionParser()
        parser.feed(
            '<div class="other">ignore</div><div class="ai-section">'
            '<h2>AI 热点分析</h2><div class="ai-block"><p>核心趋势内容</p></div>'
            '</div><div>ignore too</div>'
        )
        self.assertEqual(parser.text(), "AI 热点分析\n核心趋势内容")

    def test_cleans_markdown_for_tts(self) -> None:
        result = podcast.clean_script("# 标题\n1. 第一条\n- 第二条", 100)
        self.assertEqual(result, "标题\n第一条\n第二条")

    def test_audio_retention_keeps_exactly_ninety_days(self) -> None:
        today = date(2026, 10, 16)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            oldest_kept = root / (today - timedelta(days=89)).isoformat()
            expired = root / (today - timedelta(days=90)).isoformat()
            unrelated = root / "cache"
            for directory in (oldest_kept, expired, unrelated):
                directory.mkdir()
                (directory / "daily.mp3").write_bytes(b"audio")
            (root / "latest.json").write_text(
                json.dumps({"date": expired.name}), encoding="utf-8"
            )

            removed = podcast.cleanup_audio_history(root, today, 90)

            self.assertEqual(removed, [expired])
            self.assertTrue(oldest_kept.exists())
            self.assertTrue(unrelated.exists())
            self.assertFalse(expired.exists())
            self.assertFalse((root / "latest.json").exists())

    def test_builds_bounded_seekable_chapters(self) -> None:
        script = "\n\n".join(
            ["今天先快速看三条重要新闻。"]
            + [f"第{index}个主题有一项已经确认的事实，也有值得观察的后续。" for index in range(1, 9)]
        )

        chapters = podcast.build_chapters(script)

        self.assertLessEqual(len(chapters), 6)
        self.assertEqual(chapters[0]["title"], "今日速览")
        self.assertEqual(chapters[0]["start_seconds"], 0)
        self.assertEqual(chapters[0]["start_ratio"], 0)
        self.assertEqual(chapters[-1]["title"], "今日结语")
        self.assertEqual(
            [chapter["start_seconds"] for chapter in chapters],
            sorted(chapter["start_seconds"] for chapter in chapters),
        )
        self.assertEqual(
            [chapter["start_ratio"] for chapter in chapters],
            sorted(chapter["start_ratio"] for chapter in chapters),
        )

    def test_chapter_titles_remove_generic_transitions(self) -> None:
        chapters = podcast.build_chapters(
            "今日先看几条新闻。\n\n"
            "重点一，AI 芯片供应出现新变化，值得关注。\n\n"
            "重点三：银行已经开始针对 Agent 风险采取行动了。\n\n"
            "最后，继续观察市场变化。"
        )

        self.assertEqual(
            [chapter["title"] for chapter in chapters],
            ["今日速览", "AI 芯片供应出现新变化", "Agent 金融安全", "今日结语"],
        )

    def test_chapter_titles_prefer_factual_topics_over_filler(self) -> None:
        self.assertEqual(
            podcast.chapter_title("过去几年，AI 股票估值不断走高，但融资成本也在上升。", 1, 3),
            "AI 成本与估值",
        )
        self.assertEqual(
            podcast.chapter_title("这里我插一句，OpenCode 遭到攻击并造成 API 密钥外泄。", 1, 3),
            "开源工具与安全风险",
        )

    def test_short_script_does_not_expand_when_it_meets_minimum(self) -> None:
        class Client:
            def chat(self, *_args, **_kwargs):
                raise AssertionError("chat should not be called")

        script = "足够长的稿件"
        self.assertEqual(
            podcast.expand_short_script(Client(), script, "来源", len(script), 100),
            script,
        )


if __name__ == "__main__":
    unittest.main()
