"""Contract tests for the private daily dual-host episode bundle."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
import sys
import tempfile
import subprocess
from unittest.mock import Mock
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from podcast_dialogue_episode import DialogueBundleError, build_dialogue_bundle  # noqa: E402


def _value(command: list[str], flag: str) -> Path:
    return Path(command[command.index(flag) + 1])


def _dialogue(*, long_sentence: bool = False) -> dict:
    if long_sentence:
        line = "这是一个没有换气边界的超长句子" * 5
    else:
        line = "这是一段有清楚停顿的口播事实，接着解释它为什么值得听。" * 3
    return {
        "hosts": {
            "S1": {"name": "知远", "voice": "male_1"},
            "S2": {"name": "晓宁", "voice": "female_1"},
        },
        "text": [["S1" if index % 2 == 0 else "S2", line] for index in range(30)],
        "turn_meta": [
            {"kind": "overview" if index == 0 else "continuation",
             "topic": "topic-a" if index < 15 else "topic-b"}
            for index in range(30)
        ],
    }


class FakeRunner:
    def __init__(self, *, review: dict | None = None, metrics_status: str = "ok",
                 drop_last_turn: bool = False, reported_duration: float = 350.0):
        self.calls: list[list[str]] = []
        self.review = review or {"date": "2026-10-31", "warnings": [],
                                 "response_metadata": {"finish_reason": "stop"}}
        self.metrics_status = metrics_status
        self.drop_last_turn = drop_last_turn
        self.reported_duration = reported_duration
        self.dialogue = _dialogue()

    def __call__(self, command: list[str], **kwargs):
        self.calls.append(command)
        if command[1].endswith("generate_dialogue_podcast.py"):
            day = _value(command, "--output-root") / "2026-10-31"
            day.mkdir(parents=True, exist_ok=True)
            (day / "dialogue-draft.json").write_text(
                json.dumps(self.dialogue, ensure_ascii=False), encoding="utf-8"
            )
            (day / "writer-plan.json").write_text(json.dumps({
                "date": "2026-10-31",
                "topics": [{"source": "Example", "published_at": "2026-10-31",
                             "facts": ["A report describes a new offline test."]}],
            }, ensure_ascii=False), encoding="utf-8")
            (day / "draft-review.json").write_text(
                json.dumps(self.review, ensure_ascii=False), encoding="utf-8"
            )
            return None
        if command[1].endswith("soulx_dialogue_synthesize.py"):
            output = _value(command, "--output-dir")
            script = _value(command, "--script")
            output.mkdir(parents=True, exist_ok=True)
            (output / "dialogue.mp3").write_bytes(b"not-an-mp3-but-a-private-test-artifact")
            script_sha = hashlib.sha256(script.read_bytes()).hexdigest()
            metrics = {
                "ok": self.metrics_status == "ok",
                "status": self.metrics_status,
                "duration_ok": self.metrics_status == "ok",
                "script_sha256": script_sha,
                "total_duration_seconds": self.reported_duration,
                "reference": {"voices": {"S1": "male_1", "S2": "female_1"}},
                "device": "mps", "generation_wall_seconds": 70.0,
                "turns": [{"topic": "topic-a" if i < 15 else "topic-b",
                           "index": i, "speaker": "S1" if i % 2 == 0 else "S2",
                           "voice": "male_1" if i % 2 == 0 else "female_1",
                           "pause_before_ms": 100, "raw_audio_seconds": 11.5666666667}
                          for i in range(30)],
            }
            if self.drop_last_turn:
                metrics["turns"] = metrics["turns"][:-1]
                metrics["turn_count"] = len(metrics["turns"])
            else:
                metrics["turn_count"] = 30
            (output / "dialogue.metrics.json").write_text(
                json.dumps(metrics), encoding="utf-8"
            )
            return None
        raise AssertionError(f"unexpected command: {command}")


class DialogueEpisodeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / "trial"
        self.private = self.root / "private"
        self.stage = self.root / "stage"
        self.prompt = self.root / "prompt.txt"
        self.prompt.write_text("write a dialogue", encoding="utf-8")
        self.materials = {
            "date": "2026-10-31", "policy_version": "podcast-materials-v1",
            "items": [{"id": "a", "title": "A title", "source": "Example",
                        "url": "https://example.test/a", "published_at": "2026-10-31",
                        "text": "private source body must not be copied"}],
        }

    def _build(self, runner: FakeRunner, *, stage: Path | None = None, **kwargs):
        duration_probe = kwargs.pop("duration_probe", lambda _path: 350.0)
        return build_dialogue_bundle(
            materials=self.materials, report_date=date(2026, 10, 31),
            prompt_path=self.prompt, runtime_root=self.runtime,
            stage=stage or self.stage, private_root=self.private,
            command_runner=runner, duration_probe=duration_probe, **kwargs,
        )

    def test_valid_bundle_runs_writer_and_backend_once_and_has_public_safe_outputs(self):
        runner = FakeRunner()
        summary = self._build(runner)
        self.assertEqual(len(runner.calls), 2)
        self.assertNotIn("--force", runner.calls[0])
        self.assertNotIn("--force", runner.calls[1])
        self.assertEqual(runner.calls[0][runner.calls[0].index("--format") + 1], "plain-turns")
        self.assertTrue((self.stage / "daily.mp3").is_file())
        transcript = (self.stage / "daily.txt").read_text(encoding="utf-8")
        self.assertNotIn("[S1]", transcript)
        sources = json.loads((self.stage / "sources.json").read_text(encoding="utf-8"))
        self.assertNotIn("text", json.dumps(sources, ensure_ascii=False))
        self.assertEqual(summary["voice"], "male_1+female_1")
        self.assertEqual(summary["chapters"][1]["title"], "topic-b")

    def test_child_timeout_is_persisted_without_retry(self):
        from podcast_dialogue_episode import _run
        state = self.root / "synthesis-process.json"
        runner = Mock(side_effect=subprocess.TimeoutExpired(["python", "backend.py"], 12))
        with self.assertRaises(subprocess.TimeoutExpired):
            _run(runner, ["python", "backend.py"], timeout=12, state_path=state)
        saved = json.loads(state.read_text())
        self.assertEqual(saved["status"], "timed_out")
        self.assertEqual(saved["stage"], "backend")
        self.assertEqual(saved["error_type"], "TimeoutExpired")
        self.assertEqual(runner.call_count, 1)
        self.assertTrue(state.with_suffix(".log").is_file())

    def test_frozen_material_change_is_rejected_before_another_writer_call(self):
        first = FakeRunner()
        self._build(first)
        self.materials["items"][0]["text"] = "changed source body"
        second_stage = self.root / "stage-two"
        second = FakeRunner()
        with self.assertRaisesRegex(DialogueBundleError, "frozen materials changed"):
            self._build(second, stage=second_stage)
        self.assertEqual(second.calls, [])
        self.assertFalse(second_stage.exists() and any(second_stage.iterdir()))

    def test_short_script_blocks_tts(self):
        runner = FakeRunner()
        runner.dialogue = _dialogue()
        runner.dialogue["text"] = runner.dialogue["text"][:30]
        for index in range(30):
            runner.dialogue["text"][index][1] = "短句。"
        with self.assertRaises(DialogueBundleError):
            self._build(runner)
        self.assertEqual(len(runner.calls), 1)
        self.assertFalse(self.stage.exists() and any(self.stage.iterdir()))

    def test_long_sentence_warning_blocks_tts(self):
        runner = FakeRunner()
        runner.dialogue = _dialogue(long_sentence=True)
        with self.assertRaises(DialogueBundleError):
            self._build(runner)
        self.assertEqual(len(runner.calls), 1)

    def test_rejected_mode_blocks_tts_even_if_old_script_is_valid(self):
        runner = FakeRunner(review={"date": "2026-10-31", "warnings": [],
                                   "mode": "invalid_model_output"})
        with self.assertRaisesRegex(DialogueBundleError, "writer review rejected"):
            self._build(runner)
        self.assertEqual(len(runner.calls), 1)
        self.assertFalse(self.stage.exists() and any(self.stage.iterdir()))

    def test_truncated_writer_review_blocks_tts(self):
        runner = FakeRunner(review={"date": "2026-10-31", "warnings": [],
                                   "response_metadata": {"finish_reason": "length"}})
        with self.assertRaises(DialogueBundleError):
            self._build(runner)
        self.assertEqual(len(runner.calls), 1)

    def test_backend_failure_or_measured_duration_never_stages_partial_audio(self):
        failed = FakeRunner(metrics_status="duration_out_of_bounds")
        with self.assertRaises(DialogueBundleError):
            self._build(failed)
        self.assertEqual(len(failed.calls), 2)
        self.assertFalse(self.stage.exists() and any(self.stage.iterdir()))

        out_of_range = FakeRunner()
        with self.assertRaises(DialogueBundleError):
            self._build(out_of_range, stage=self.root / "other-stage",
                        duration_probe=lambda _path: 601.0)
        self.assertEqual(len(out_of_range.calls), 2)

    def test_incomplete_turn_telemetry_never_stages_audio(self):
        runner = FakeRunner(drop_last_turn=True)
        with self.assertRaisesRegex(DialogueBundleError, "telemetry"):
            self._build(runner)
        self.assertEqual(len(runner.calls), 2)
        self.assertFalse(self.stage.exists() and any(self.stage.iterdir()))

    def test_reported_duration_disagreement_never_stages_audio(self):
        runner = FakeRunner(reported_duration=500.0)
        with self.assertRaisesRegex(DialogueBundleError, "disagrees"):
            self._build(runner, duration_probe=lambda _path: 310.0)
        self.assertEqual(len(runner.calls), 2)
        self.assertFalse(self.stage.exists() and any(self.stage.iterdir()))

    def test_production_private_root_is_rejected(self):
        runner = FakeRunner()
        with self.assertRaises(DialogueBundleError):
            build_dialogue_bundle(
                materials=self.materials, report_date=date(2026, 10, 31),
                prompt_path=self.prompt, runtime_root=self.runtime, stage=self.stage,
                private_root=Path("/Users/bruce/Coding/NewsRadar/TrendRadar/output"),
                command_runner=runner, duration_probe=lambda _path: 350.0,
            )
        self.assertEqual(runner.calls, [])


if __name__ == "__main__":
    unittest.main()
