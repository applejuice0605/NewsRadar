from __future__ import annotations

import importlib.util
import json
import re
import sys
import tempfile
import types
import unittest
import wave
from unittest import mock
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "soulx_synthesize.py"
SPEC = importlib.util.spec_from_file_location("soulx_synthesize", MODULE_PATH)
assert SPEC and SPEC.loader
soulx = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(soulx)
HAS_NUMPY = importlib.util.find_spec("numpy") is not None


class FakeRuntime:
    def __init__(self, fail_after: int | None = None):
        self.calls: list[str] = []
        self.fail_after = fail_after

    def generate(self, text: str):
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise RuntimeError("synthetic generation failure")
        self.calls.append(text)
        # A non-silent, dependency-free fake waveform.  The production path
        # converts the same shape from a torch tensor/numpy array.
        return [0.15, -0.10, 0.05, -0.02]


class SoulXSynthesizeTest(unittest.TestCase):
    def test_chunks_preserve_all_characters_and_protect_versions(self) -> None:
        text = (
            "第一段先说明背景。这里包含 3.14 和 v1.2，不能从小数或版本号中间断开。"
            "第二段继续说明已确认事实，也要保留 English API v2.0 的原样字符。"
        ) * 8
        chunks = soulx.split_text(text, min_chars=45, max_chars=75)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), text)
        self.assertEqual(
            "".join(char for char in "".join(chunks) if not char.isspace()),
            "".join(char for char in text if not char.isspace()),
        )
        for left, right in zip(chunks, chunks[1:]):
            joined = left + right
            self.assertNotRegex(joined, r"3\.$")
            self.assertNotRegex(joined, r"v1\.$")

    def test_unpunctuated_cjk_is_hard_bounded(self) -> None:
        text = "汉字" * 300
        chunks = soulx.split_text(text, min_chars=45, max_chars=75)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(chunk) <= 75 for chunk in chunks[:-1]))

    def test_overlong_ascii_token_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsplittable ASCII token"):
            soulx.split_text("A" * 300, min_chars=45, max_chars=75)

    def test_normalize_speaker_input_preserves_full_parser_payload(self) -> None:
        body = "第一行\n\n中文 API 2.0 在 2026 年继续测试。"
        normalized = soulx.normalize_speaker_input(body)
        self.assertEqual("".join(normalized.split()), "".join(body.split()))
        self.assertNotRegex(normalized, r"[\r\n\t\f\v]")
        parser_payload = f"[S1]{normalized}"
        match = re.match(r"(\[S[1-9]\])(.+)", parser_payload)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.group(2), normalized)

    def test_normalize_speaker_input_rejects_marker_injection(self) -> None:
        with self.assertRaisesRegex(ValueError, "reserved speaker marker"):
            soulx.normalize_speaker_input("正常正文\n[S1] 注入 [S2] 另一段")

    def test_official_prosody_tags_are_preserved_and_unknown_tags_rejected(self) -> None:
        body = "这句允许自然换气。<|breathing|> 下一句保留笑声。<|laughter|>"
        normalized = soulx.normalize_speaker_input(body)
        self.assertEqual(normalized, body)
        self.assertEqual(soulx._speech_length(body), len("这句允许自然换气。 下一句保留笑声。"))
        with self.assertRaisesRegex(ValueError, "unsupported SoulX prosody tag"):
            soulx.normalize_speaker_input("正文。<|sighing|>")
        with self.assertRaisesRegex(ValueError, "spoken text"):
            soulx.normalize_speaker_input("<|laughter|>")

    def test_prosody_tags_are_atomic_and_never_a_standalone_chunk(self) -> None:
        text = "甲" * 38 + "<|breathing|>" + "乙" * 38
        chunks = soulx.split_text(text, min_chars=20, max_chars=40)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(any("<|breathing|>" in chunk for chunk in chunks))
        self.assertTrue(all(soulx._speech_length(chunk) > 0 for chunk in chunks))
        self.assertTrue(all("<|breathing|>" not in chunk or chunk.count("<|breathing|>") == 1 for chunk in chunks))

    def test_runtime_passes_official_tag_through_parser_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prompts = root / "prompt_audios"
            prompts.mkdir()
            (prompts / "male_1.wav").write_bytes(b"prompt")
            (prompts / "male_1.txt").write_text("提示文本", encoding="utf-8")
            captured: dict[str, object] = {}

            def process(*args):
                captured["args"] = args
                return {"payload": True}

            class FakeTensor:
                def __init__(self, values):
                    self.values = values

                def squeeze(self):
                    return self

                def reshape(self, *_shape):
                    return self

                def __iter__(self):
                    return iter(self.values)

            class FakeTorch:
                @staticmethod
                def cat(values, dim):
                    return FakeTensor([0.1, 0.2])

            runtime = soulx.SoulXRuntime(root, root / "model", root / "cache", "male_1")
            runtime.model = type("Model", (), {"forward_longform": lambda self, **data: {"generated_wavs": [[[0.1, 0.2]]]}})()
            runtime.dataset = object()
            runtime._torch = FakeTorch()
            runtime._process_single_input = process
            generated = runtime.generate("一句话。<|sigh|>")
            self.assertEqual(captured["args"][1], ["[S1]一句话。<|sigh|>"])
            self.assertEqual(list(generated.reshape(-1)), [0.1, 0.2])

    @unittest.skipUnless(HAS_NUMPY, "numpy required for waveform/cache integration")
    def test_paragraph_silence_is_exact_and_not_per_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            model = root / "model"
            source.mkdir()
            model.mkdir()
            (source / "revision.py").write_text("one", encoding="utf-8")
            (model / "config.json").write_text("one", encoding="utf-8")
            script = "第一段内容。\n\n第二段内容。"
            report = soulx.synthesize(
                script,
                root / "paragraph.wav",
                source_dir=source,
                model_dir=model,
                cache_dir=root / "cache",
                voice="male_1",
                runtime=FakeRuntime(),
                min_chars=2,
                max_chars=240,
            )
            with wave.open(str(root / "paragraph.wav"), "rb") as wav:
                frames = wav.readframes(wav.getnframes())
                self.assertEqual(wav.getnframes(), 4 + 4 + 9600)
            import numpy as np

            pcm = np.frombuffer(frames, dtype=np.int16)
            self.assertTrue(np.all(pcm[4 : 4 + 9600] == 0))
            self.assertEqual([item["pause_before_ms"] for item in report["chunks"]], [0, 400])

    def test_job_fingerprint_changes_when_source_or_model_reference_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            model = root / "model"
            source.mkdir()
            model.mkdir()
            (source / "revision.py").write_text("one", encoding="utf-8")
            (model / "config.json").write_text("one", encoding="utf-8")
            first, _ = soulx.job_fingerprint(
                "稿件。", source_dir=source, model_dir=model, voice="male_1"
            )
            _, reference = soulx.job_fingerprint(
                "稿件。", source_dir=source, model_dir=model, voice="male_1"
            )
            self.assertEqual(reference["backend_version"], soulx.BACKEND_VERSION)
            self.assertTrue(reference["backend_sha256"])
            self.assertIn("torch", reference)
            (source / "revision.py").write_text("two", encoding="utf-8")
            second, _ = soulx.job_fingerprint(
                "稿件。", source_dir=source, model_dir=model, voice="male_1"
            )
            self.assertNotEqual(first, second)
            (model / "config.json").write_text("two", encoding="utf-8")
            third, _ = soulx.job_fingerprint(
                "稿件。", source_dir=source, model_dir=model, voice="male_1"
            )
            self.assertNotEqual(second, third)

    @unittest.skipUnless(HAS_NUMPY, "numpy required for waveform/cache integration")
    def test_failure_does_not_publish_or_replace_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            model = root / "model"
            cache = root / "cache"
            source.mkdir()
            model.mkdir()
            (source / "revision.py").write_text("one", encoding="utf-8")
            (model / "config.json").write_text("one", encoding="utf-8")
            output = root / "podcast.wav"
            metrics = root / "podcast.json"
            output.write_bytes(b"old wav")
            metrics.write_bytes(b"old metrics")
            with self.assertRaises(RuntimeError):
                soulx.synthesize(
                    "第一句很长，足以让测试切成一个独立的小段。" * 8,
                    output,
                    source_dir=source,
                    model_dir=model,
                    cache_dir=cache,
                    voice="male_1",
                    runtime=FakeRuntime(fail_after=1),
                    min_chars=20,
                    max_chars=35,
                    metrics_path=metrics,
                )
            self.assertEqual(output.read_bytes(), b"old wav")
            self.assertEqual(metrics.read_bytes(), b"old metrics")
            self.assertFalse(list(root.glob(".*.tmp")))

    @unittest.skipUnless(HAS_NUMPY, "numpy required for waveform/cache integration")
    def test_success_publishes_wav_metrics_and_reuses_chunk_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            model = root / "model"
            cache = root / "cache"
            source.mkdir()
            model.mkdir()
            (source / "revision.py").write_text("one", encoding="utf-8")
            (model / "config.json").write_text("one", encoding="utf-8")
            script = "这是一个用于离线缓存验证的句子。" * 5
            runtime = FakeRuntime()
            report = soulx.synthesize(
                script,
                root / "podcast.wav",
                source_dir=source,
                model_dir=model,
                cache_dir=cache,
                voice="female_1",
                runtime=runtime,
                min_chars=20,
                max_chars=35,
            )
            self.assertTrue(report["ok"])
            self.assertTrue((root / "podcast.wav").is_file())
            metrics = json.loads((root / "podcast.json").read_text(encoding="utf-8"))
            self.assertEqual(metrics["chunk_count"], len(runtime.calls))
            second_runtime = FakeRuntime()
            second = soulx.synthesize(
                script,
                root / "podcast-2.wav",
                source_dir=source,
                model_dir=model,
                cache_dir=cache,
                voice="female_1",
                runtime=second_runtime,
                min_chars=20,
                max_chars=35,
            )
            self.assertEqual(second_runtime.calls, [])
            self.assertTrue(all(item["cache_hit"] for item in second["chunks"]))

    @unittest.skipUnless(HAS_NUMPY, "numpy required for waveform/cache integration")
    def test_tagged_chunk_cache_reuses_without_runtime_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            model = root / "model"
            source.mkdir()
            model.mkdir()
            (source / "revision.py").write_text("one", encoding="utf-8")
            (model / "config.json").write_text("one", encoding="utf-8")
            script = "这是一段带标签的测试文本。<|breathing|> 后面继续。" * 2
            soulx.synthesize(
                script,
                root / "tagged-1.wav",
                source_dir=source,
                model_dir=model,
                cache_dir=root / "cache",
                voice="male_1",
                runtime=FakeRuntime(),
                min_chars=20,
                max_chars=35,
            )
            second_runtime = FakeRuntime()
            second = soulx.synthesize(
                script,
                root / "tagged-2.wav",
                source_dir=source,
                model_dir=model,
                cache_dir=root / "cache",
                voice="male_1",
                runtime=second_runtime,
                min_chars=20,
                max_chars=35,
            )
            self.assertEqual(second_runtime.calls, [])
            self.assertTrue(all(item["cache_hit"] for item in second["chunks"]))

    def test_runtime_fails_closed_without_mps(self) -> None:
        fake_torch = types.SimpleNamespace(
            backends=types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: False))
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prompts = root / "source" / "prompt_audios"
            prompts.mkdir(parents=True)
            (prompts / "male_1.wav").write_bytes(b"prompt")
            (prompts / "male_1.txt").write_text("提示文本", encoding="utf-8")
            model = root / "model"
            model.mkdir()
            with mock.patch.dict(sys.modules, {"torch": fake_torch}):
                with self.assertRaisesRegex(RuntimeError, "MPS is unavailable"):
                    soulx.SoulXRuntime(root / "source", model, root / "cache", "male_1").load()

    def test_memory_checkpoint_uses_platform_rss_units(self) -> None:
        usage = types.SimpleNamespace(ru_maxrss=123)
        fake_torch = types.SimpleNamespace(
            backends=types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: False))
        )
        with mock.patch.object(soulx.resource, "getrusage", return_value=usage):
            with mock.patch.dict(sys.modules, {"torch": fake_torch}):
                with mock.patch.object(soulx.sys, "platform", "darwin"):
                    self.assertEqual(soulx._memory_checkpoint()["rss_bytes"], 123)
                with mock.patch.object(soulx.sys, "platform", "linux"):
                    self.assertEqual(soulx._memory_checkpoint()["rss_bytes"], 123 * 1024)


if __name__ == "__main__":
    unittest.main()
