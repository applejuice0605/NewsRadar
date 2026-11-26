from __future__ import annotations

import importlib.util
import fcntl
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock
import wave


MODULE_PATH = Path("/Users/bruce/Coding/NewsRadar/scripts/soulx_dialogue_synthesize.py")
SPEC = importlib.util.spec_from_file_location("soulx_dialogue_synthesize", MODULE_PATH)
assert SPEC and SPEC.loader
dialogue = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dialogue)


class _FakeMPS:
    @staticmethod
    def synchronize() -> None:
        return None


class _FakeTorch:
    mps = _FakeMPS()


class _FakeLLM:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._past = object()

    def generate(self, *args, **kwargs):
        self.calls.append(kwargs)
        return [0.1, -0.1]


class _FakeModel:
    def __init__(self) -> None:
        self.llm = _FakeLLM()

    def forward_longform(self, **data):
        generated = []
        for index, text in enumerate(data["input_texts"]):
            # Exercise the telemetry wrapper with a new incoming cache object
            # after the first turn.
            self.llm.generate(input_ids=list(range(index + 1)), past_key_values=(None if index == 0 else object()))
            generated.append([0.1, -0.1, 0.05, -0.05])
        return {"generated_wavs": generated}


class _FakeRuntime:
    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail
        self.model = _FakeModel()

    def load_context(self, source, model, cache, voices):
        self.calls += 1
        if self.fail:
            raise AssertionError("cache miss unexpectedly invoked the fake model")

        def process(dataset, input_texts, prompt_wavs, prompt_texts, flag, extras):
            self.input_texts = input_texts
            self.prompt_wavs = prompt_wavs
            self.prompt_texts = prompt_texts
            self.dialect_prompts = extras
            return {"input_texts": input_texts}

        return self.model, object(), _FakeTorch(), process


class SoulXDialogueTest(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path]:
        runtime = root / "runtime"
        (runtime / ".venv" / "bin").mkdir(parents=True)
        (runtime / ".venv" / "bin" / "python").write_text("fake", encoding="utf-8")
        source = runtime / "source" / "prompt_audios"
        source.mkdir(parents=True)
        for voice in ("male_1", "female_1"):
            (source / f"{voice}.wav").write_bytes(b"prompt")
            (source / f"{voice}.txt").write_text("普通话提示", encoding="utf-8")
        (runtime / "models" / "SoulX-Podcast-1.7B-dialect").mkdir(parents=True)
        (runtime / "models" / "SoulX-Podcast-1.7B-dialect" / "config.json").write_text("v1", encoding="utf-8")
        (runtime / "cache").mkdir()
        turns = []
        for index in range(6):
            body = ("这是第%d轮，用来验证双人节目候选后端的原始音频、语义停顿和缓存行为。" % index) * 10
            turns.append(["S1" if index % 2 == 0 else "S2", body])
        script = root / "script.json"
        script.write_text(json.dumps({
            "hosts": {"S1": {"name": "知远", "voice": "male_1"}, "S2": {"name": "晓宁", "voice": "female_1"}},
            "turn_meta": [
                {"kind": "overview", "topic": "a"}, {"kind": "backchannel", "topic": "a"},
                {"kind": "question", "topic": "a"}, {"kind": "answer", "topic": "a"},
                {"kind": "topic_transition", "topic": "b"}, {"kind": "closing", "topic": "b"},
            ], "text": turns,
        }, ensure_ascii=False), encoding="utf-8")
        return runtime, script

    def _args(self, runtime: Path, script: Path, output: Path) -> Namespace:
        return Namespace(script=script, output_dir=output, runtime_root=runtime,
                         coordination_lock=None, min_seconds=0, max_seconds=10)

    def test_consecutive_same_speaker_keeps_reference_voice_and_turns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, script = self._fixture(root)
            data = json.loads(script.read_text())
            data["text"][1][0] = "S1"
            data["text"][2][0] = "S2"
            script.write_text(json.dumps(data, ensure_ascii=False))
            fake = _FakeRuntime()
            report = dialogue.run(self._args(runtime, script, root/"out"), runtime=fake)
            self.assertTrue(report["ok"])
            self.assertEqual([x["speaker"] for x in report["turns"]], [x[0] for x in data["text"]])
            self.assertEqual([x["voice"] for x in report["turns"][:3]], ["male_1", "male_1", "female_1"])
            self.assertEqual(fake.input_texts, [f"[{speaker}]{text}" for speaker,text in data["text"]])

    def test_semantic_pauses_preserve_raw_pcm_and_emit_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, script = self._fixture(root)
            output = root / "out"
            fake = _FakeRuntime()
            report = dialogue.run(self._args(runtime, script, output), runtime=fake)
            self.assertTrue(report["ok"])
            self.assertEqual(fake.calls, 1)
            self.assertEqual(len(fake.prompt_wavs), 2)
            self.assertEqual(len(fake.prompt_texts), 2)
            self.assertEqual(fake.dialect_prompts, ["", ""])
            self.assertEqual(len(fake.input_texts), 6)
            self.assertEqual(len(report["turns"]), 6)
            self.assertEqual([row["pause_reason"] for row in report["turns"]],
                             ["opening", "backchannel", "question", "answer", "topic_transition", "closing"])
            self.assertEqual(len(report["telemetry"]), 6)
            self.assertFalse(report["telemetry"][0]["context_reset"])
            self.assertTrue(report["telemetry"][1]["context_reset"])
            self.assertEqual(report["telemetry"][3]["memory_checkpoint"].keys(),
                             {"rss_bytes", "mps_allocated_bytes", "mps_driver_bytes"})
            with wave.open(str(output / "model-raw.wav"), "rb") as raw, wave.open(str(output / "dialogue.wav"), "rb") as candidate:
                raw_pcm = raw.readframes(raw.getnframes())
                candidate_pcm = candidate.readframes(candidate.getnframes())
            # Both files are PCM16 and candidate speech sections are exactly
            # the corresponding raw sections; only inserted silence differs.
            cursor = 0
            for row in report["turns"]:
                silence = round(24000 * row["pause_before_ms"] / 1000) * 2
                self.assertEqual(candidate_pcm[cursor:cursor + silence], b"\0" * silence)
                cursor += silence
                length = row["raw_frames"] * 2
                self.assertEqual(candidate_pcm[cursor:cursor + length], raw_pcm[:length])
                raw_pcm = raw_pcm[length:]
                cursor += length

    def test_cache_reuses_complete_job_without_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, script = self._fixture(root)
            first = dialogue.run(self._args(runtime, script, root / "first"), runtime=_FakeRuntime())
            self.assertTrue(first["ok"])
            second_runtime = _FakeRuntime(fail=True)
            second = dialogue.run(self._args(runtime, script, root / "second"), runtime=second_runtime)
            self.assertTrue(second["ok"])
            self.assertTrue(second["cache_hit"])
            self.assertEqual(second_runtime.calls, 0)

    def test_duration_miss_reuses_complete_raw_job_without_gpu(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, script = self._fixture(root)
            first_args = self._args(runtime, script, root / "first")
            first_args.min_seconds = 5
            first_args.max_seconds = 10
            first = dialogue.run(first_args, runtime=_FakeRuntime())
            self.assertFalse(first["ok"])
            self.assertEqual(first["status"], "duration_out_of_bounds")
            second_runtime = _FakeRuntime(fail=True)
            second_args = self._args(runtime, script, root / "second")
            second_args.min_seconds = 5
            second_args.max_seconds = 10
            second = dialogue.run(second_args, runtime=second_runtime)
            self.assertFalse(second["ok"])
            self.assertTrue(second["cache_hit"])
            self.assertEqual(second_runtime.calls, 0)

    def test_unknown_tag_marker_and_busy_lock_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, script = self._fixture(root)
            document = json.loads(script.read_text(encoding="utf-8"))
            document["text"][0][1] = document["text"][0][1][:-1] + "<|sighing|>"
            script.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unsupported SoulX prosody tag"):
                dialogue.parse_script(script)
            document["text"][0][1] = "正常正文 [S2]" + "啊" * 350
            script.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "reserved speaker marker"):
                dialogue.parse_script(script)
            # Hold the runtime lock with an independent descriptor; a
            # persistent lock file alone is not a busy condition.
            lock = runtime / "cache" / ".gpu.lock"
            lock_handle = lock.open("a+")
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            # restore valid fixture script before exercising the lock path
            _, script = self._fixture(root / "lock-fixture")
            try:
                with self.assertRaisesRegex(RuntimeError, "busy"):
                    dialogue.run(self._args(runtime, script, root / "busy-out"), runtime=_FakeRuntime())
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()

            # A coordination lock must also block the runner, and a runtime
            # lock acquired first must be released when the second is busy.
            _, script = self._fixture(root / "coord-fixture")
            coordination = root / "coord.lock"
            coordination_handle = coordination.open("a+")
            fcntl.flock(coordination_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            args = self._args(runtime, script, root / "coord-busy-out")
            args.coordination_lock = coordination
            try:
                with self.assertRaisesRegex(RuntimeError, "busy"):
                    dialogue.run(args, runtime=_FakeRuntime())
            finally:
                fcntl.flock(coordination_handle.fileno(), fcntl.LOCK_UN)
                coordination_handle.close()

    def test_known_production_audio_roots_are_rejected(self) -> None:
        for production_root in dialogue.KNOWN_PRODUCTION_OUTPUT_ROOTS:
            with self.assertRaisesRegex(ValueError, "production output root"):
                dialogue._safe_output_dir(production_root)
            with self.assertRaisesRegex(ValueError, "production output root"):
                dialogue._safe_output_dir(production_root / "html" / "candidate")


    def test_telemetry_does_not_retain_old_kv_cache(self) -> None:
        import gc
        import weakref
        from types import SimpleNamespace

        class Cache:
            pass

        class LLM:
            def generate(self, *args, **kwargs):
                return {"token_ids": [1]}

        model = SimpleNamespace(llm=LLM())
        telemetry = []
        dialogue._wrap_generate(model, telemetry)
        first = Cache()
        reference = weakref.ref(first)
        with mock.patch.object(dialogue, "_memory_checkpoint", return_value={}):
            model.llm.generate([1], past_key_values=first)
            # Construct the next native cache before releasing the current
            # one, as forward_longform does on reset; their IDs must differ.
            second = Cache()
            del first
            gc.collect()
            self.assertIsNone(reference())
            model.llm.generate([2], past_key_values=second)
        self.assertFalse(telemetry[0]["context_reset"])
        self.assertTrue(telemetry[1]["context_reset"])


if __name__ == "__main__":
    unittest.main()
