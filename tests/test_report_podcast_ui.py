import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


class ReportPodcastUiTests(unittest.TestCase):
    def test_only_ai_podcast_controls_remain(self) -> None:
        script = (REPO / "docs" / "assets" / "report-speech.js").read_text(encoding="utf-8")
        self.assertIn("AI 播客", script)
        self.assertIn("matchesReport(metadata)", script)
        self.assertIn("'/audio/' + pathMatch[1] + '/daily.json'", script)
        self.assertIn("云扬 · 中文男声", script)
        self.assertIn("晓晓 · 中文女声", script)
        self.assertIn("今日播客暂未生成", script)
        self.assertIn("podcast-chapters", script)
        self.assertIn("chapter.start_ratio", script)
        self.assertIn("loadedmetadata", script)
        self.assertIn("audio.duration - 0.25", script)
        self.assertIn("is-mini", script)
        self.assertNotIn("speechSynthesis", script)
        self.assertNotIn("开始朗读", script)
        self.assertNotIn("speechRate", script)

    def test_podcast_metadata_has_friendly_voice_label(self) -> None:
        generator = (REPO / "scripts" / "generate_daily_podcast.py").read_text(encoding="utf-8")
        self.assertIn('"voice_label"', generator)

    def test_recommendations_expand_only_above_five_items(self) -> None:
        script = (REPO / "docs" / "assets" / "report-experience.js").read_text(encoding="utf-8")
        self.assertIn("DEFAULT_RECOMMENDATION_LIMIT = 5", script)
        self.assertIn("articles.slice(0, DEFAULT_RECOMMENDATION_LIMIT)", script)
        self.assertIn("total <= DEFAULT_RECOMMENDATION_LIMIT", script)
        self.assertNotIn("展开所有重点依据", script)


if __name__ == "__main__":
    unittest.main()
