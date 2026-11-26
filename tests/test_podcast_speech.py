"""Regression tests for the podcast long-sentence speech-load heuristic."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("podcast_speech_test", ROOT / "scripts" / "podcast_speech.py")
assert SPEC and SPEC.loader
podcast_speech = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(podcast_speech)

ENGLISH_WORD_UNITS = podcast_speech.ENGLISH_WORD_UNITS
spoken_sentence_units = podcast_speech.spoken_sentence_units


class SpokenSentenceUnitsTest(unittest.TestCase):
    def test_real_mixed_sentence_no_longer_false_positives(self) -> None:
        text = "官方称，ML4在Artificial Analysis Coding Agent Index中的得分为48%，与GLM-5.3基本持平"
        self.assertGreater(len(text), 65)  # legacy clause gate includes spaces
        self.assertLess(spoken_sentence_units(text), 65)

    def test_cjk_count_is_unchanged(self) -> None:
        text = "汉" * 66
        self.assertEqual(spoken_sentence_units(text), 66)
        self.assertGreater(spoken_sentence_units(text), 65)

    def test_short_word_and_uppercase_abbreviation(self) -> None:
        self.assertAlmostEqual(spoken_sentence_units("agent"), ENGLISH_WORD_UNITS)
        self.assertEqual(spoken_sentence_units("AI"), 2)
        self.assertEqual(spoken_sentence_units("ML4"), 3)

    def test_long_english_sentence_still_triggers(self) -> None:
        text = " ".join(["agent"] * 36)
        self.assertGreater(spoken_sentence_units(text), 65)

    def test_apostrophe_digits_and_punctuation_are_retained(self) -> None:
        self.assertAlmostEqual(
            spoken_sentence_units("don't"), ENGLISH_WORD_UNITS + 1
        )
        self.assertEqual(spoken_sentence_units("GLM-5.3"), 7)
        self.assertEqual(spoken_sentence_units("3.5%"), 4)


if __name__ == "__main__":
    unittest.main()
