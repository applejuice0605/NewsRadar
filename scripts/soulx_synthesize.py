"""Offline, bounded SoulX podcast synthesis.

The module deliberately keeps the SoulX/torch imports lazy.  This makes the
chunking, cache, and publication safeguards usable in the normal NewsRadar
test environment, where the large local model is not installed.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import resource
import shutil
import sys
import tempfile
import time
import wave
from pathlib import Path
from typing import Any, Iterable, Sequence


SAMPLE_RATE = 24_000
DEFAULT_MIN_CHARS = 180
DEFAULT_MAX_CHARS = 240
VOICES = ("male_1", "female_1")
BACKEND_VERSION = "soulx-synthesize-v3-prosody-pauses"
PARAGRAPH_SILENCE_MS = 400
PAUSE_CONFIG = {
    "paragraph_ms": PARAGRAPH_SILENCE_MS,
    "topic_ms": 0,
    "policy": "paragraph-boundaries-only",
}
SPEAKER_MARKER_RE = re.compile(r"\[S[1-9]\]")
PROSODY_TAGS = (
    "<|laughter|>",
    "<|sigh|>",
    "<|breathing|>",
    "<|coughing|>",
    "<|throat_clearing|>",
)
PROSODY_TAG_SET = frozenset(PROSODY_TAGS)
PROSODY_TAG_RE = re.compile(r"<\|[^|<>]*\|>")
PROSODY_FRAGMENT_RE = re.compile(r"<\||\|>")


def _non_whitespace(text: str) -> str:
    return "".join(char for char in text if not char.isspace())


def _prosody_tag_spans(text: str) -> list[tuple[int, int]]:
    """Return validated official SoulX prosody tag spans.

    The model tokenizer contains other internal/experimental tokens (for
    example ``<|sighing|>``).  They are deliberately rejected here so an AI
    writer cannot silently depend on an unsupported token.
    """

    spans: list[tuple[int, int]] = []
    for match in PROSODY_TAG_RE.finditer(text):
        token = match.group(0)
        if token not in PROSODY_TAG_SET:
            raise ValueError(f"unsupported SoulX prosody tag: {token}")
        spans.append((match.start(), match.end()))
    if PROSODY_FRAGMENT_RE.search(text):
        # Covers malformed/unknown forms such as ``<|laugh|`` which would
        # otherwise be passed as ordinary text and create an accidental tag.
        covered = {position for start, end in spans for position in range(start, end)}
        for match in PROSODY_FRAGMENT_RE.finditer(text):
            if match.start() not in covered:
                raise ValueError("malformed or unsupported SoulX prosody tag")
    return spans


def _speech_length(text: str) -> int:
    """Count chunk characters excluding official prosody markup."""

    return sum(len(fragment) for fragment in _non_overlapping_spans(text, _prosody_tag_spans(text)))


def _non_overlapping_spans(text: str, spans: list[tuple[int, int]]) -> Iterable[str]:
    cursor = 0
    for start, end in spans:
        if start > cursor:
            yield text[cursor:start]
        cursor = end
    if cursor < len(text):
        yield text[cursor:]


def _validate_script_markup(text: str, *, require_speech: bool = True) -> list[tuple[int, int]]:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("script text must contain at least one non-whitespace character")
    if SPEAKER_MARKER_RE.search(text):
        raise ValueError("script body contains reserved speaker marker [S1]..[S9]")
    spans = _prosody_tag_spans(text)
    if require_speech and not _non_whitespace("".join(_non_overlapping_spans(text, spans))):
        raise ValueError("script text must contain spoken text in addition to prosody tags")
    return spans


def normalize_speaker_input(text: str) -> str:
    """Make one speaker's parser input single-line without changing words.

    SoulX's parser uses a non-DOTALL regex, so raw newlines would truncate a
    chunk at its first line.  Replace every whitespace character with an
    ordinary space while preserving every non-whitespace character.  Speaker
    tags are reserved syntax; rejecting them in user prose prevents accidental
    marker injection from becoming a second turn.
    """

    _validate_script_markup(text, require_speech=True)
    normalized = "".join(" " if char.isspace() else char for char in text)
    if _non_whitespace(normalized) != _non_whitespace(text):
        raise AssertionError("speaker normalization changed script characters")
    _validate_script_markup(normalized, require_speech=True)
    return normalized


def _safe_period(text: str, position: int) -> bool:
    """Return whether the full stop at ``position`` is a useful boundary.

    A dot between digits (``3.14``) or between ASCII letters/digits
    (``v1.2``, ``U.S.``) is not treated as a sentence ending.
    """

    if position <= 0 or position + 1 >= len(text):
        return True
    left, right = text[position - 1], text[position + 1]
    if left.isdigit() and right.isdigit():
        return False
    if left.isascii() and right.isascii() and (left.isalnum() and right.isalnum()):
        return False
    return True


def _boundary_positions(text: str, start: int, end: int) -> tuple[list[int], list[int]]:
    """Return preferred sentence/paragraph and softer punctuation boundaries."""

    preferred: list[int] = []
    soft: list[int] = []
    sentence_marks = "。！？!?"
    soft_marks = "，,；;：:"
    index = start
    while index < end:
        char = text[index]
        cut: int | None = None
        if char in sentence_marks or (char == "." and _safe_period(text, index)):
            cut = index + 1
            while cut < end and text[cut].isspace():
                cut += 1
            preferred.append(cut)
        elif char in soft_marks:
            cut = index + 1
            while cut < end and text[cut].isspace():
                cut += 1
            soft.append(cut)
        index += 1

    # Paragraph and line boundaries are useful even without punctuation.
    for match in re.finditer(r"\n[ \t]*\n+|\n", text[start:end]):
        cut = start + match.end()
        if cut <= end:
            preferred.append(cut)
    return sorted(set(preferred)), sorted(set(soft))


def _whitespace_cut(text: str, start: int, limit: int) -> int | None:
    """Find the latest safe word boundary at or before ``limit``."""

    for position in range(min(limit, len(text)), start, -1):
        if position < len(text) and text[position - 1].isspace():
            return position
    return None


def _ascii_token_char(char: str) -> bool:
    return char.isascii() and (char.isalnum() or char in "._-")


def _ascii_token_span(text: str, position: int) -> tuple[int, int] | None:
    """Return the complete ASCII token containing ``position``, if any."""

    if not text or position < 0 or position >= len(text) or not _ascii_token_char(text[position]):
        return None
    left = position
    while left > 0 and _ascii_token_char(text[left - 1]):
        left -= 1
    right = position + 1
    while right < len(text) and _ascii_token_char(text[right]):
        right += 1
    return left, right


def _raw_limit_for_speech_count(text: str, start: int, target: int, spans: list[tuple[int, int]]) -> int:
    """Map a speech-character budget to a raw boundary without splitting tags."""
    if target < 1:
        return start
    span_index = next((index for index, (_, end) in enumerate(spans) if end > start), len(spans))
    position = start
    count = 0
    while position < len(text):
        if span_index < len(spans) and spans[span_index][0] == position:
            position = spans[span_index][1]
            span_index += 1
            continue
        position += 1
        count += 1
        if count >= target:
            while span_index < len(spans) and spans[span_index][0] == position:
                position = spans[span_index][1]
                span_index += 1
            return position
    return len(text)


def _paragraph_boundary_positions(text: str, start: int, end: int) -> list[int]:
    positions: list[int] = []
    for match in re.finditer(r"\n[ \t]*\n+", text[start:end]):
        cut = start + match.end()
        if cut <= end:
            positions.append(cut)
    return sorted(set(positions))


def _dependency_version(name: str) -> str:
    """Read an installed dependency version without importing the dependency."""

    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unavailable"


def split_text(
    text: str,
    *,
    min_chars: int = DEFAULT_MIN_CHARS,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> list[str]:
    """Split text without changing its characters.

    Sentence/paragraph boundaries are preferred.  Long sentences fall back
    to commas/whitespace, while decimal and Latin-version dots are protected.
    The returned chunks concatenate to the input exactly.
    """

    _validate_script_markup(text, require_speech=True)
    if min_chars < 1 or max_chars < min_chars:
        raise ValueError("chunk bounds are invalid")
    spans = _prosody_tag_spans(text)

    chunks: list[str] = []
    start = 0
    while start < len(text):
        remaining = _speech_length(text[start:])
        if remaining <= max_chars:
            paragraph_cuts = _paragraph_boundary_positions(text, start + 1, len(text))
            if paragraph_cuts:
                cut = paragraph_cuts[0]
                chunks.append(text[start:cut])
                start = cut
                continue
            chunks.append(text[start:])
            break

        limit = _raw_limit_for_speech_count(text, start, max_chars, spans)
        floor = _raw_limit_for_speech_count(text, start, min_chars, spans)
        preferred, soft = _boundary_positions(text, start + 1, limit)

        # Prefer a boundary in the natural target window.  If a paragraph or
        # sentence is unusually short, use it only when it will not strand a
        # tiny remainder; otherwise combine it with following prose.
        paragraph_candidates = _paragraph_boundary_positions(text, start + 1, limit)
        candidates = paragraph_candidates
        if not candidates:
            candidates = [p for p in preferred if floor <= p <= limit]
        if not candidates:
            candidates = [p for p in soft if floor <= p <= limit]
        # Structural paragraph cuts are intentionally earliest-first: every
        # blank-line boundary must survive, even when several short
        # paragraphs fit inside one nominal chunk budget.
        cut = (min(candidates) if paragraph_candidates else max(candidates)) if candidates else None
        if cut is None:
            cut = _whitespace_cut(text, start, limit)
        if cut is None or cut <= start:
            # Chinese (and other non-ASCII prose) has no lexical whitespace,
            # so hard-cut it at the bound.  An ASCII token is different:
            # splitting a decimal/version or identifier is worse than asking
            # the caller to add a safe boundary, and an overlong token must
            # never silently turn into an unbounded chunk.
            span = _ascii_token_span(text, max(start, limit - 1))
            if span is None:
                span = _ascii_token_span(text, min(limit, len(text) - 1))
            if span is not None:
                token_start, token_end = span
                token_length = token_end - token_start
                if token_length > max_chars:
                    raise ValueError(
                        "script contains an unsplittable ASCII token longer than "
                        f"max_chars={max_chars}: positions {token_start}:{token_end}"
                    )
                if token_start > start and token_start >= floor:
                    cut = token_start
                elif token_end <= limit:
                    cut = token_end
                else:
                    raise ValueError(
                        "no safe chunk boundary before an ASCII token; add whitespace "
                        "or punctuation near the chunk limit"
                    )
            else:
                cut = limit
            for tag_start, tag_end in spans:
                if tag_start < cut < tag_end:
                    cut = tag_end
                    break
        chunks.append(text[start:cut])
        start = cut

    if _non_whitespace("".join(chunks)) != _non_whitespace(text):
        raise AssertionError("chunking changed script characters")
    index = 0
    while index < len(chunks):
        if _speech_length(chunks[index]) == 0:
            if index:
                chunks[index - 1] += chunks[index]
                del chunks[index]
                continue
            if index + 1 < len(chunks):
                chunks[index + 1] = chunks[index] + chunks[index + 1]
                del chunks[index]
                continue
        index += 1
    return chunks


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def _iter_files(root: Path) -> Iterable[Path]:
    if root.is_file():
        yield root
        return
    for path in sorted(root.rglob("*")):
        if path.is_file() and ".git" not in path.parts and "__pycache__" not in path.parts:
            yield path


def revision_fingerprint(root: Path, *, content: bool = False) -> str:
    """Fingerprint a source/model reference without requiring a VCS checkout.

    Source files are hashed by content.  Large model weights use size and
    nanosecond mtime, which detects replacement/revision changes without
    reading gigabytes at every podcast invocation.
    """

    root = Path(root).resolve()
    digest = hashlib.sha256()
    if not root.exists():
        raise FileNotFoundError(root)
    for path in _iter_files(root):
        rel = path.name if root.is_file() else str(path.relative_to(root))
        stat = path.stat()
        digest.update(rel.encode("utf-8"))
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(str(stat.st_mtime_ns).encode("ascii"))
        if content:
            digest.update(path.read_bytes())
        elif stat.st_size <= 1_048_576:
            digest.update(path.read_bytes())
    return digest.hexdigest()


def job_fingerprint(
    text: str,
    *,
    source_dir: Path,
    model_dir: Path,
    voice: str,
    min_chars: int = DEFAULT_MIN_CHARS,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> tuple[str, dict[str, Any]]:
    source_fp = revision_fingerprint(source_dir, content=True)
    model_fp = revision_fingerprint(model_dir, content=False)
    reference = {
        "backend_version": BACKEND_VERSION,
        "backend_sha256": _sha256_bytes(Path(__file__).resolve().read_bytes()),
        "script_sha256": sha256_text(text),
        "source_fingerprint": source_fp,
        "model_fingerprint": model_fp,
        "voice": voice,
        "engine": "hf",
        "device": "mps",
        "sample_rate": SAMPLE_RATE,
        "min_chars": min_chars,
        "max_chars": max_chars,
        "pause_config": PAUSE_CONFIG,
        "python": platform.python_version(),
        "torch": _dependency_version("torch"),
    }
    encoded = json.dumps(reference, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return _sha256_bytes(encoded), reference


def _atomic_replace_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _audio_array(audio: Any):
    """Convert a SoulX tensor/array to a finite mono float32 numpy array."""

    import numpy as np  # intentionally lazy

    if hasattr(audio, "detach"):
        audio = audio.detach().float().cpu().numpy()
    array = np.asarray(audio, dtype=np.float32).reshape(-1)
    if array.size == 0 or not np.isfinite(array).all():
        raise ValueError("SoulX produced empty or non-finite audio")
    peak = float(np.max(np.abs(array)))
    if peak <= 1e-7:
        raise ValueError("SoulX produced silent audio")
    return np.clip(array, -1.0, 1.0)


def _wav_bytes(audio: Any, sample_rate: int = SAMPLE_RATE) -> bytes:
    import numpy as np  # intentionally lazy

    array = _audio_array(audio)
    pcm = (array * 32767.0).astype(np.int16).tobytes()
    from io import BytesIO

    stream = BytesIO()
    with wave.open(stream, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return stream.getvalue()


def _read_wav(path: Path):
    import numpy as np  # intentionally lazy

    with wave.open(str(path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getframerate() != SAMPLE_RATE:
            raise ValueError(f"invalid cached wav format: {path}")
        payload = wav.readframes(wav.getnframes())
    array = np.frombuffer(payload, dtype=np.int16).astype(np.float32) / 32767.0
    return _audio_array(array)


def pause_after_chunk(previous: str, following: str, *, paragraph_ms: int = PARAGRAPH_SILENCE_MS) -> int:
    """Return a pause only for an explicit blank-line paragraph boundary."""

    if paragraph_ms < 0:
        raise ValueError("paragraph_ms must be non-negative")
    if re.search(r"\n[ \t]*\n+$", previous) or re.match(r"^\n[ \t]*\n", following):
        return paragraph_ms
    return 0


def _silence_frames(duration_ms: int, sample_rate: int = SAMPLE_RATE) -> int:
    if duration_ms < 0:
        raise ValueError("silence duration must be non-negative")
    return round(sample_rate * duration_ms / 1000)


def _memory_checkpoint() -> dict[str, int | None]:
    rss = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    # macOS reports ru_maxrss in bytes; Linux reports KiB.  Keep the raw
    # macOS value instead of applying the Linux conversion twice.
    if sys.platform != "darwin":
        rss *= 1024
    result: dict[str, int | None] = {"rss_bytes": rss, "mps_allocated_bytes": None, "mps_driver_bytes": None}
    try:
        import torch  # intentionally lazy

        if torch.backends.mps.is_available():
            result["mps_allocated_bytes"] = int(torch.mps.current_allocated_memory())
            result["mps_driver_bytes"] = int(torch.mps.driver_allocated_memory())
    except (ImportError, AttributeError, RuntimeError):
        pass
    return result


class SoulXRuntime:
    """One loaded HF/MPS model and one fixed voice prompt."""

    def __init__(self, source_dir: Path, model_dir: Path, cache_dir: Path, voice: str):
        self.source_dir = Path(source_dir).resolve()
        self.model_dir = Path(model_dir).resolve()
        self.cache_dir = Path(cache_dir).resolve()
        self.voice = voice
        self.model = None
        self.dataset = None
        self._torch = None
        self._process_single_input = None
        self.prompt_wav = self.source_dir / "prompt_audios" / f"{voice}.wav"
        self.prompt_text_path = self.source_dir / "prompt_audios" / f"{voice}.txt"

    def load(self) -> "SoulXRuntime":
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        os.environ.update(
            {
                # Match the known-good trial layout: s3tokenizer looks under
                # XDG_CACHE_HOME/s3tokenizer rather than HF_HOME.
                "HF_HOME": str(self.cache_dir.parent / "hf-cache"),
                "XDG_CACHE_HOME": str(self.cache_dir),
                "TORCH_HOME": str(self.cache_dir / "torch"),
                "NUMBA_CACHE_DIR": str(self.cache_dir / "numba"),
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "HF_DATASETS_OFFLINE": "1",
                "HF_HUB_DISABLE_TELEMETRY": "1",
                "HF_HUB_DISABLE_XET": "1",
            }
        )
        if not self.prompt_wav.is_file() or not self.prompt_text_path.is_file():
            raise FileNotFoundError(f"missing local SoulX prompt for {self.voice}")
        if str(self.source_dir) not in sys.path:
            sys.path.insert(0, str(self.source_dir))
        import torch

        self._torch = torch
        if not torch.backends.mps.is_available():
            raise RuntimeError("MPS is unavailable; refusing silent CPU fallback")
        from soulxpodcast.utils.infer_utils import initiate_model, process_single_input

        self.model, self.dataset = initiate_model(1988, str(self.model_dir), "hf", False)
        self._process_single_input = process_single_input
        if str(getattr(self.model.llm, "device", "")) != "mps":
            raise RuntimeError("SoulX HF model did not load on MPS")
        return self

    def generate(self, text: str):
        if self.model is None or self.dataset is None or self._process_single_input is None:
            self.load()
        prompt_text = self.prompt_text_path.read_text(encoding="utf-8").strip()
        parser_text = normalize_speaker_input(text)
        data = self._process_single_input(
            self.dataset,
            [f"[S1]{parser_text}"],
            [str(self.prompt_wav)],
            [prompt_text],
            False,
            [""],
        )
        result = self.model.forward_longform(**data)
        generated = result.get("generated_wavs") or []
        if not generated:
            raise ValueError("SoulX returned no audio")
        return self._torch.cat(generated, dim=1).squeeze()


def synthesize(
    text: str,
    output: Path,
    *,
    source_dir: Path,
    model_dir: Path,
    cache_dir: Path,
    voice: str,
    runtime: Any | None = None,
    min_chars: int = DEFAULT_MIN_CHARS,
    max_chars: int = DEFAULT_MAX_CHARS,
    metrics_path: Path | None = None,
) -> dict[str, Any]:
    """Synthesize text and publish WAV + metrics only after all chunks pass guards."""

    if voice not in VOICES:
        raise ValueError(f"voice must be one of: {', '.join(VOICES)}")
    # Check the complete ledger before chunking so a marker cannot evade the
    # guard by being split across two otherwise-valid chunks.
    if SPEAKER_MARKER_RE.search(text):
        raise ValueError("script body contains reserved speaker marker [S1]..[S9]")
    output = Path(output).resolve()
    source_dir, model_dir, cache_dir = map(lambda p: Path(p).resolve(), (source_dir, model_dir, cache_dir))
    chunks = split_text(text, min_chars=min_chars, max_chars=max_chars)
    job_id, reference = job_fingerprint(
        text, source_dir=source_dir, model_dir=model_dir, voice=voice,
        min_chars=min_chars, max_chars=max_chars,
    )
    job_dir = cache_dir / "soulx-jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = job_dir / "manifest.json"
    manifest = {
        "version": 2,
        "job_id": job_id,
        "reference": reference,
        "pause_config": PAUSE_CONFIG,
        "chunks": {},
    }
    if manifest_path.is_file():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
            if previous.get("reference") == reference and previous.get("job_id") == job_id:
                manifest["chunks"] = previous.get("chunks", {})
        except (OSError, ValueError, TypeError):
            pass

    if runtime is None:
        runtime = SoulXRuntime(source_dir, model_dir, cache_dir, voice).load()
    elif hasattr(runtime, "load") and getattr(runtime, "model", object()) is None:
        runtime.load()

    output.parent.mkdir(parents=True, exist_ok=True)
    metrics_path = Path(metrics_path).resolve() if metrics_path else output.with_suffix(".json")
    output_tmp = output.parent / f".{output.name}.{os.getpid()}.tmp"
    metrics_tmp = metrics_path.parent / f".{metrics_path.name}.{os.getpid()}.tmp"
    start_time = time.perf_counter()
    chunk_metrics: list[dict[str, Any]] = []
    total_frames = 0
    try:
        with wave.open(str(output_tmp), "wb") as final_wav:
            final_wav.setnchannels(1)
            final_wav.setsampwidth(2)
            final_wav.setframerate(SAMPLE_RATE)
            for index, chunk in enumerate(chunks):
                pause_ms = (
                    pause_after_chunk(chunks[index - 1], chunk)
                    if index
                    else 0
                )
                pause_frames = _silence_frames(pause_ms)
                if pause_frames:
                    final_wav.writeframes(b"\0" * pause_frames * 2)
                    total_frames += pause_frames
                chunk_key = str(index)
                chunk_sha = sha256_text(chunk)
                cached = manifest["chunks"].get(chunk_key, {})
                chunk_path = job_dir / f"chunk-{index:05d}.wav"
                cached_length_ok = cached.get("text_length") == _speech_length(chunk)
                cached_raw_length_ok = cached.get("raw_text_length") == len(chunk)
                if cached.get("text_sha256") == chunk_sha and cached_length_ok and cached_raw_length_ok and chunk_path.is_file():
                    audio = _read_wav(chunk_path)
                    cache_hit = True
                else:
                    generated = runtime.generate(chunk)
                    audio = _audio_array(generated)
                    _atomic_replace_bytes(chunk_path, _wav_bytes(audio))
                    cache_hit = False
                audio_bytes = _wav_bytes(audio)
                frames = len(audio)
                final_wav.writeframes(audio_bytes[44:])
                total_frames += frames
                checkpoint = _memory_checkpoint()
                item = {
                    "index": index,
                    "text_sha256": chunk_sha,
                    "text_length": _speech_length(chunk),
                    "raw_text_length": len(chunk),
                    "prosody_tag_count": len(_prosody_tag_spans(chunk)),
                    "pause_before_ms": pause_ms,
                    "audio_seconds": frames / SAMPLE_RATE,
                    "cache_hit": cache_hit,
                    **checkpoint,
                }
                manifest["chunks"][chunk_key] = item
                chunk_metrics.append(item)
                _atomic_replace_bytes(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
        elapsed = time.perf_counter() - start_time
        report = {
            "ok": True,
            "job_id": job_id,
            "script_sha256": reference["script_sha256"],
            "source_fingerprint": reference["source_fingerprint"],
            "model_fingerprint": reference["model_fingerprint"],
            "voice": voice,
            "engine": "hf",
            "device": "mps",
            "sample_rate": SAMPLE_RATE,
            "chunks": chunk_metrics,
            "chunk_count": len(chunks),
            "audio_seconds": total_frames / SAMPLE_RATE,
            "silence_seconds": sum(item["pause_before_ms"] for item in chunk_metrics) / 1000,
            "pause_config": PAUSE_CONFIG,
            "elapsed_seconds": elapsed,
            "rtf": elapsed / (total_frames / SAMPLE_RATE) if total_frames else None,
            "cache_dir": str(job_dir),
        }
        _atomic_replace_bytes(metrics_tmp, json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))
        os.replace(output_tmp, output)
        os.replace(metrics_tmp, metrics_path)
        return report
    except BaseException:
        for temporary in (output_tmp, metrics_tmp):
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline local SoulX podcast synthesis")
    parser.add_argument("--text-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--model-dir", required=True, type=Path)
    parser.add_argument("--cache-dir", required=True, type=Path)
    parser.add_argument("--voice", choices=VOICES, required=True)
    parser.add_argument("--metrics-json", type=Path)
    args = parser.parse_args(argv)
    text = args.text_file.read_text(encoding="utf-8")
    report = synthesize(
        text,
        args.output,
        source_dir=args.source_dir,
        model_dir=args.model_dir,
        cache_dir=args.cache_dir,
        voice=args.voice,
        metrics_path=args.metrics_json,
    )
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
