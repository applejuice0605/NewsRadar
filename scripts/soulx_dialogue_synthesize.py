"""Offline, reproducible two-host SoulX audition backend.

This module is deliberately separate from the daily podcast worker.  It is a
candidate-only runner: one native ``process_single_input`` call prepares all
turns and one ``forward_longform`` call generates them.  The model's PCM is
kept byte-for-byte in ``model-raw.wav``; only externally inserted semantic
silence is changed when the candidate WAV is assembled.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import resource
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from typing import Any, Callable, Mapping, Sequence


NEWSRADAR = Path("/Users/bruce/Coding/NewsRadar")
SYNTH_PATH = Path(__file__).resolve().with_name("soulx_synthesize.py")
SAMPLE_RATE = 24_000
BACKEND_VERSION = "soulx-dialogue-audition-v1"
VOICE_BY_SPEAKER = {"S1": "male_1", "S2": "female_1"}
NAME_BY_SPEAKER = {"S1": "知远", "S2": "晓宁"}
TURN_RE = re.compile(r"^S[12]$")
SCRIPT_MIN_CHARS = 1800
SCRIPT_MAX_CHARS = 2600
KNOWN_PRODUCTION_AUDIO = NEWSRADAR / "TrendRadar" / "output" / "audio"
KNOWN_PRODUCTION_OUTPUT_ROOTS = (
    NEWSRADAR / "TrendRadar" / "output",
    Path("/Users/bruce/Coding/NewsRadar-production/TrendRadar/output"),
)

# These are deterministic editorial defaults.  They are inserted silence,
# never an operation on the model PCM.  A topic change takes precedence over
# the current turn's kind.
PAUSE_MS = {
    "overview": 250,
    "question": 180,
    "answer": 250,
    "default": 250,
    "backchannel": 150,
    "joke": 220,
    "closing": 280,
    "continuation": 220,
    "question_response": 180,
    "interrupt": 60,
    "topic_transition": 700,
}
KNOWN_KINDS = frozenset(PAUSE_MS)


def _load_synth_module() -> Any:
    spec = importlib.util.spec_from_file_location("newsradar_soulx_synthesize", SYNTH_PATH)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load synthesis helpers: {SYNTH_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _audio_array(audio: Any):
    import numpy as np

    if hasattr(audio, "detach"):
        audio = audio.detach().float().cpu().numpy()
    array = np.asarray(audio, dtype=np.float32)
    # SoulX returns singleton batch/channel dimensions.  A genuine multi-
    # channel tensor is rejected instead of silently flattening it into mono.
    if array.ndim > 1:
        non_singleton = [size for size in array.shape if size != 1]
        if len(non_singleton) != 1:
            raise ValueError("SoulX generated audio must be mono")
        array = array.reshape(-1)
    else:
        array = array.reshape(-1)
    if array.size == 0 or not np.isfinite(array).all():
        raise ValueError("SoulX produced empty or non-finite audio")
    if float(np.max(np.abs(array))) <= 1e-7:
        raise ValueError("SoulX produced silent audio")
    return np.clip(array, -1.0, 1.0)


def _wav_payload(array: Any) -> bytes:
    import io
    import numpy as np

    data = _audio_array(array)
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes((data * 32767.0).astype(np.int16).tobytes())
    return stream.getvalue()


def _read_wav_frames(path: Path) -> tuple[wave._wave_params, bytes]:
    with wave.open(str(path), "rb") as source:
        params = source.getparams()
        if params.nchannels != 1 or params.sampwidth != 2 or params.framerate != SAMPLE_RATE:
            raise ValueError(f"invalid dialogue WAV format: {path}")
        return params, source.readframes(source.getnframes())


def _write_wav(path: Path, frame_chunks: Sequence[bytes]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    frames = 0
    try:
        with os.fdopen(fd, "wb") as raw:
            with wave.open(raw, "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(SAMPLE_RATE)
                for chunk in frame_chunks:
                    output.writeframes(chunk)
                    frames += len(chunk) // 2
                output.close()
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    return frames


def _write_mp3(wav_path: Path, mp3_path: Path) -> None:
    mp3_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav_path),
         "-codec:a", "libmp3lame", "-q:a", "2", str(mp3_path)],
        check=True,
    )


def _memory_checkpoint() -> dict[str, int | None]:
    rss = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    if sys.platform != "darwin":
        rss *= 1024
    result: dict[str, int | None] = {
        "rss_bytes": rss,
        "mps_allocated_bytes": None,
        "mps_driver_bytes": None,
    }
    try:
        import torch

        if torch.backends.mps.is_available():
            result["mps_allocated_bytes"] = int(torch.mps.current_allocated_memory())
            result["mps_driver_bytes"] = int(torch.mps.driver_allocated_memory())
    except (ImportError, AttributeError, RuntimeError):
        pass
    return result


class ExclusiveFileLock:
    """A persistent, non-blocking advisory flock shared with production."""

    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self.owned = False
        self._handle: Any | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            handle.close()
            if isinstance(exc, BlockingIOError) or getattr(exc, "errno", None) in {11, 35}:
                raise RuntimeError(f"busy: lock is already held: {self.path}") from exc
            raise
        self._handle = handle
        self.owned = True

    def release(self) -> None:
        if self.owned:
            if self._handle is not None:
                try:
                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
                finally:
                    self._handle.close()
                self._handle = None
            self.owned = False


def _safe_output_dir(path: Path) -> Path:
    output = path.resolve()
    for production_root in KNOWN_PRODUCTION_OUTPUT_ROOTS:
        production = production_root.resolve()
        if output == production or production in output.parents:
            raise ValueError(f"candidate output cannot be production output root: {output}")
    output.mkdir(parents=True, exist_ok=True)
    return output


def _validate_runtime_root(runtime_root: Path) -> tuple[Path, Path, Path]:
    root = runtime_root.resolve()
    source = root / "source"
    model = root / "models" / "SoulX-Podcast-1.7B-dialect"
    cache = root / "cache"
    for label, path in (("runtime source", source), ("SoulX model", model), ("runtime cache", cache)):
        if not path.is_dir():
            raise FileNotFoundError(f"missing {label}: {path}")
    python = root / ".venv" / "bin" / "python"
    if not python.is_file():
        raise FileNotFoundError(f"missing SoulX runtime Python: {python}")
    return source, model, cache


def _validate_source(source: Path, voices: Mapping[str, str]) -> None:
    for voice in voices.values():
        wav = source / "prompt_audios" / f"{voice}.wav"
        text = source / "prompt_audios" / f"{voice}.txt"
        if not wav.is_file() or not text.is_file():
            raise FileNotFoundError(f"missing local SoulX prompt pair for {voice}")
        if not text.read_text(encoding="utf-8").strip():
            raise ValueError(f"empty SoulX prompt text for {voice}")


def _turn_meta(document: Mapping[str, Any], count: int) -> list[dict[str, Any]]:
    raw = document.get("turn_meta")
    if raw is None:
        return [{"kind": "default", "topic": None} for _ in range(count)]
    if isinstance(raw, list):
        if len(raw) != count:
            raise ValueError("turn_meta list must have one item per text turn")
        values = raw
    elif isinstance(raw, dict):
        values = [raw.get(str(index), raw.get(index, {})) for index in range(count)]
    else:
        raise ValueError("turn_meta must be a list or index-keyed object")
    result = []
    for index, item in enumerate(values):
        if not isinstance(item, dict):
            raise ValueError(f"turn_meta[{index}] must be an object")
        kind = item.get("kind", "default")
        if not isinstance(kind, str) or kind not in KNOWN_KINDS:
            raise ValueError(f"turn_meta[{index}].kind must be one of {sorted(KNOWN_KINDS)}")
        topic = item.get("topic")
        if topic is not None and (not isinstance(topic, str) or not topic.strip()):
            raise ValueError(f"turn_meta[{index}].topic must be a non-empty string or null")
        result.append({"kind": kind, "topic": topic})
    return result


def parse_script(path: Path, synth: Any | None = None, *, bulletin: bool = False) -> dict[str, Any]:
    print("[soulx-stage] loading_local_helpers", flush=True)
    synth = synth or _load_synth_module()
    script_path = Path(path).resolve()
    raw = script_path.read_bytes()
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"script must be UTF-8 JSON: {script_path}") from exc
    if not isinstance(document, dict):
        raise ValueError("script root must be an object")
    hosts = document.get("hosts")
    if not isinstance(hosts, dict):
        raise ValueError("script requires hosts.S1 and hosts.S2")
    host_values: dict[str, dict[str, str]] = {}
    for speaker, expected_voice in VOICE_BY_SPEAKER.items():
        item = hosts.get(speaker)
        if not isinstance(item, dict):
            raise ValueError(f"hosts.{speaker} must be an object")
        voice = item.get("voice")
        if voice != expected_voice:
            raise ValueError(f"hosts.{speaker}.voice must be {expected_voice}")
        name = item.get("name", NAME_BY_SPEAKER[speaker])
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"hosts.{speaker}.name must be non-empty")
        host_values[speaker] = {"name": name, "voice": voice}
    text = document.get("text")
    if not isinstance(text, list) or not text:
        raise ValueError("script requires a non-empty text list")
    turns: list[dict[str, str]] = []
    bodies: list[str] = []
    for index, item in enumerate(text):
        if not isinstance(item, list) or len(item) != 2:
            raise ValueError(f"text[{index}] must be [\"S1\"|\"S2\", \"line\"]")
        speaker, body = item
        if not isinstance(speaker, str) or not TURN_RE.fullmatch(speaker):
            raise ValueError(f"text[{index}] speaker must be S1 or S2")
        if not isinstance(body, str) or not body.strip():
            raise ValueError(f"text[{index}] line must contain text")
        normalized = synth.normalize_speaker_input(body)
        turns.append({"speaker": speaker, "text": normalized})
        bodies.append(normalized)
    speech_chars = sum(synth._speech_length(body) for body in bodies)
    minimum = SCRIPT_MIN_CHARS
    if bulletin:
        from podcast_episode_policy import BULLETIN_MIN_CHARS
        minimum = BULLETIN_MIN_CHARS
    if not minimum <= speech_chars <= SCRIPT_MAX_CHARS:
        raise ValueError(f"script speech length must be {minimum}..{SCRIPT_MAX_CHARS} characters; got {speech_chars}")
    metadata = _turn_meta(document, len(turns))
    return {
        "document": document,
        "raw": raw,
        "hosts": host_values,
        "turns": turns,
        "turn_meta": metadata,
        "speech_chars": speech_chars,
        "script_sha256": _sha256(raw),
    }


def pause_plan(turns: Sequence[Mapping[str, str]], metadata: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, (turn, meta) in enumerate(zip(turns, metadata)):
        if index == 0:
            pause, reason = 0, "opening"
        elif (
            metadata[index].get("topic") != metadata[index - 1].get("topic")
            and (metadata[index].get("topic") is not None or metadata[index - 1].get("topic") is not None)
        ):
            pause, reason = PAUSE_MS["topic_transition"], "topic_transition"
        else:
            kind = str(meta.get("kind", "default"))
            pause, reason = PAUSE_MS[kind], kind
        result.append({"pause_before_ms": pause, "pause_reason": reason, "kind": meta.get("kind", "default"), "topic": meta.get("topic")})
    return result


def _token_count(value: Any) -> int | None:
    try:
        if hasattr(value, "numel"):
            return int(value.numel())
        return len(value)
    except (TypeError, AttributeError):
        return None


def _wrap_generate(model: Any, telemetry: list[dict[str, Any]]) -> None:
    llm = getattr(model, "llm", None)
    original = getattr(llm, "generate", None)
    if llm is None or not callable(original):
        return
    if getattr(original, "_soulx_dialogue_wrapped", False):
        return
    previous_past_id: int | None = None
    call_index = 0

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        nonlocal previous_past_id, call_index
        started = time.perf_counter()
        past = kwargs.get("past_key_values")
        if past is None:
            # Keep positional handling conservative: generation APIs differ;
            # inspecting but not rewriting args avoids changing inference.
            past = None
        token_input = kwargs.get("input_ids", kwargs.get("inputs", args[0] if args else None))
        result = original(*args, **kwargs)
        current_id = id(past) if past is not None else None
        prior_id = previous_past_id
        telemetry.append({
            "turn_index": call_index,
            "wall_seconds": time.perf_counter() - started,
            "input_token_count": _token_count(token_input),
            "context_reset": bool(call_index > 0 and current_id != prior_id),
            "incoming_past_key_values_present": past is not None,
            "memory_checkpoint": _memory_checkpoint(),
        })
        print(f"model turn {call_index + 1} complete: wall={telemetry[-1]['wall_seconds']:.2f}s", flush=True)
        previous_past_id = current_id
        call_index += 1
        return result

    wrapped._soulx_dialogue_wrapped = True  # type: ignore[attr-defined]
    wrapped._soulx_dialogue_original = original  # type: ignore[attr-defined]
    llm.generate = wrapped


def _load_model(source: Path, model_dir: Path, cache: Path, voices: Mapping[str, str]) -> tuple[Any, Any, Any, Callable[..., Any]]:
    os.environ.update({
        "HF_HOME": str(cache.parent / "hf-cache"), "XDG_CACHE_HOME": str(cache),
        "TORCH_HOME": str(cache / "torch"), "NUMBA_CACHE_DIR": str(cache / "numba"),
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1", "HF_HUB_DISABLE_XET": "1",
    })
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    import torch

    if not torch.backends.mps.is_available():
        raise RuntimeError("MPS is unavailable; refusing silent CPU fallback")
    from soulxpodcast.utils.infer_utils import initiate_model, process_single_input

    model, dataset = initiate_model(1988, str(model_dir), "hf", False)
    if str(getattr(model.llm, "device", "")) != "mps":
        raise RuntimeError("SoulX HF model did not load on MPS")
    return model, dataset, torch, process_single_input


def _cache_reference(script: dict[str, Any], source: Path, model: Path, voices: Mapping[str, str], synth: Any, min_seconds: float, max_seconds: float) -> tuple[str, dict[str, Any]]:
    source_fp = synth.revision_fingerprint(source, content=True)
    model_fp = synth.revision_fingerprint(model, content=False)
    reference = {
        "backend_version": BACKEND_VERSION,
        "backend_sha256": _sha256(Path(__file__).read_bytes()),
        "helper_backend_version": getattr(synth, "BACKEND_VERSION", "unknown"),
        "script_sha256": script["script_sha256"],
        "source_fingerprint": source_fp,
        "model_fingerprint": model_fp,
        "voices": dict(voices),
        "sample_rate": SAMPLE_RATE,
        "pause_ms": PAUSE_MS,
    }
    return _sha256(json.dumps(reference, sort_keys=True, ensure_ascii=False).encode()), reference


def _complete_cache(job_dir: Path, job_id: str, reference: Mapping[str, Any]) -> bool:
    manifest_path = job_dir / "dialogue.metrics.json"
    raw_path = job_dir / "model-raw.wav"
    if not manifest_path.is_file() or not raw_path.is_file():
        return False
    try:
        report = json.loads(manifest_path.read_text(encoding="utf-8"))
        if report.get("status") not in {"ok", "duration_out_of_bounds"} or report.get("job_id") != job_id or report.get("reference") != reference:
            return False
        turns = report.get("turns")
        if not isinstance(turns, list) or len(turns) != report.get("turn_count"):
            return False
        _read_wav_frames(raw_path)
        required = [job_dir / "dialogue.wav", job_dir / "dialogue.mp3", job_dir / "dialogue.transcript.txt", job_dir / "dialogue.review.json"]
        return all(path.is_file() for path in required) and all((job_dir / f"raw-turn-{int(row['index']):04d}.wav").is_file() for row in turns)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False


def _copy_cache(job_dir: Path, output: Path, min_seconds: float, max_seconds: float) -> dict[str, Any]:
    for source in job_dir.iterdir():
        if source.is_file() and source.name != "dialogue.metrics.json":
            shutil.copy2(source, output / source.name)
    report = json.loads((job_dir / "dialogue.metrics.json").read_text(encoding="utf-8"))
    report["cache_hit"] = True
    report["min_seconds"] = min_seconds
    report["max_seconds"] = max_seconds
    report["duration_ok"] = min_seconds <= float(report["total_duration_seconds"]) <= max_seconds
    report["ok"] = bool(report["duration_ok"])
    report["status"] = "ok" if report["ok"] else "duration_out_of_bounds"
    report["outputs"] = {key: str(output / Path(value).name) for key, value in report.get("outputs", {}).items()}
    _atomic_bytes(output / "dialogue.metrics.json", (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode())
    review = {
        "ok": report["ok"],
        "review_status": "pending_human_audition" if report["ok"] else "duration_out_of_bounds",
        "raw_pcm_preserved": True,
        "speech_rate_changed": False,
    }
    _atomic_bytes(output / "dialogue.review.json", (json.dumps(review, ensure_ascii=False, indent=2) + "\n").encode())
    return report


def _failure_report(output: Path, error: BaseException, *, script: dict[str, Any] | None = None, job_id: str | None = None, reference: Mapping[str, Any] | None = None, job_dir: Path | None = None) -> dict[str, Any]:
    if job_dir is not None and job_dir.is_dir():
        for source_file in job_dir.iterdir():
            if source_file.is_file():
                shutil.copy2(source_file, output / source_file.name)
    report = {
        "ok": False, "audition_only": True, "status": "generation_failed", "error": f"{type(error).__name__}: {error}",
        "job_id": job_id, "reference": reference, "script_sha256": script.get("script_sha256") if script else None,
        "outputs": {"output_dir": str(output)}, "paid_api_calls": 0,
    }
    _atomic_bytes(output / "dialogue.metrics.json", (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode())
    _atomic_bytes(output / "dialogue.review.json", (json.dumps({"ok": False, "status": "generation_failed", "error": report["error"]}, ensure_ascii=False, indent=2) + "\n").encode())
    return report


def run(args: argparse.Namespace, *, runtime: Any | None = None, synth: Any | None = None) -> dict[str, Any]:
    print("[soulx-stage] loading_local_helpers", flush=True)
    synth = synth or _load_synth_module()
    output = _safe_output_dir(Path(args.output_dir))
    print("[soulx-stage] validating_script_and_runtime", flush=True)
    script = parse_script(Path(args.script), synth, bulletin=getattr(args, "bulletin", False))
    source, model_dir, cache = _validate_runtime_root(Path(args.runtime_root))
    voices = {speaker: data["voice"] for speaker, data in script["hosts"].items()}
    _validate_source(source, voices)
    min_seconds = float(args.min_seconds)
    max_seconds = float(args.max_seconds)
    if min_seconds < 0 or max_seconds < min_seconds:
        raise ValueError("duration bounds are invalid")
    job_id, reference = _cache_reference(script, source, model_dir, voices, synth, min_seconds, max_seconds)
    job_dir = cache / "soulx-dialogue-jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    _atomic_bytes(job_dir / "script.json", script["raw"])
    lock_paths = [cache / ".gpu.lock"]
    if args.coordination_lock:
        coordination_path = Path(args.coordination_lock).resolve()
        if coordination_path != cache.joinpath(".gpu.lock").resolve():
            lock_paths.append(coordination_path)
    # Acquire both locks in a stable path order.  If the second is busy, the
    # first is released immediately; this prevents trial/production races and
    # lock-order deadlocks with another cooperating worker.
    locks = [ExclusiveFileLock(path) for path in sorted(lock_paths, key=lambda item: str(Path(item).resolve()))]
    acquired: list[ExclusiveFileLock] = []
    try:
        try:
            for lock in locks:
                lock.acquire()
                acquired.append(lock)
        except BaseException:
            for lock in reversed(acquired):
                lock.release()
            raise
        if _complete_cache(job_dir, job_id, reference):
            return _copy_cache(job_dir, output, min_seconds, max_seconds)
        print("[soulx-stage] gpu_lock_acquired_loading_model", flush=True)
        turns, metadata = script["turns"], script["turn_meta"]
        plan = pause_plan(turns, metadata)
        telemetry: list[dict[str, Any]] = []
        started = time.perf_counter()
        try:
            if runtime is None:
                model, dataset, torch, process_single_input = _load_model(source, model_dir, cache, voices)
                _wrap_generate(model, telemetry)
            else:
                model, dataset, torch, process_single_input = runtime.load_context(source, model_dir, cache, voices)
                _wrap_generate(model, telemetry)
            target_texts = [f"[{row['speaker']}]{row['text']}" for row in turns]
            # Prompt conditioning is fixed by the two hosts, not repeated per
            # turn.  The [S1]/[S2] markers in target_texts select one of these
            # two references for every generated turn.
            prompt_wavs = [str(source / "prompt_audios" / f"{voices[speaker]}.wav") for speaker in ("S1", "S2")]
            prompt_texts = [(source / "prompt_audios" / f"{voices[speaker]}.txt").read_text(encoding="utf-8").strip() for speaker in ("S1", "S2")]
            # Exactly one native preprocessing call and one model call.
            data = process_single_input(dataset, target_texts, prompt_wavs, prompt_texts, False, ["", ""])
            result = model.forward_longform(**data)
            if hasattr(torch, "mps"):
                try:
                    torch.mps.synchronize()
                except (AttributeError, RuntimeError):
                    pass
            generated = result.get("generated_wavs") or []
            if len(generated) != len(turns):
                raise RuntimeError(f"SoulX returned {len(generated)} turns for {len(turns)} inputs")
            arrays = [_audio_array(item) for item in generated]
            raw_payloads = [_wav_payload(item)[44:] for item in arrays]
            raw_frames = [len(payload) // 2 for payload in raw_payloads]
            if not all(raw_frames):
                raise ValueError("SoulX returned an empty turn")
            raw_path = job_dir / "model-raw.wav"
            _write_wav(raw_path, raw_payloads)
            for index, payload in enumerate(raw_payloads):
                _write_wav(job_dir / f"raw-turn-{index:04d}.wav", [payload])
            candidate_chunks: list[bytes] = []
            turn_reports: list[dict[str, Any]] = []
            for index, (row, payload, frames, pause) in enumerate(zip(turns, raw_payloads, raw_frames, plan)):
                pause_frames = round(SAMPLE_RATE * pause["pause_before_ms"] / 1000)
                candidate_chunks.append(bytes(pause_frames * 2))
                candidate_chunks.append(payload)
                turn_reports.append({
                    "index": index, "speaker": row["speaker"], "voice": voices[row["speaker"]],
                    "text_sha256": _sha256(row["text"].encode()), "text_length": synth._speech_length(row["text"]),
                    "raw_frames": frames, "raw_audio_seconds": frames / SAMPLE_RATE,
                    "audio_seconds": frames / SAMPLE_RATE, **pause,
                })
                print(f"turn {index + 1}/{len(turns)} ready: {row['speaker']} raw={frames / SAMPLE_RATE:.2f}s pause={pause['pause_before_ms']}ms", flush=True)
            candidate_path = job_dir / "dialogue.wav"
            total_frames = _write_wav(candidate_path, candidate_chunks)
            _write_mp3(candidate_path, job_dir / "dialogue.mp3")
            transcript = "\n".join(f"{script['hosts'][row['speaker']]['name']}：{synth.PROSODY_TAG_RE.sub('', row['text'])}" for row in turns) + "\n"
            (job_dir / "dialogue.transcript.txt").write_text(transcript, encoding="utf-8")
            elapsed = time.perf_counter() - started
            duration = total_frames / SAMPLE_RATE
            duration_ok = min_seconds <= duration <= max_seconds
            report = {
                "ok": duration_ok, "audition_only": True, "status": "ok" if duration_ok else "duration_out_of_bounds",
                "job_id": job_id, "reference": reference, "script_sha256": script["script_sha256"],
                "source_fingerprint": reference["source_fingerprint"], "model_fingerprint": reference["model_fingerprint"],
                "backend_version": BACKEND_VERSION, "device": "mps", "sample_rate": SAMPLE_RATE,
                "turn_count": len(turn_reports), "speech_chars": script["speech_chars"], "turns": turn_reports,
                "total_duration_seconds": duration, "min_seconds": min_seconds, "max_seconds": max_seconds,
                "duration_ok": duration_ok, "generation_wall_seconds": elapsed, "telemetry": telemetry,
                "outputs": {"wav": str(output / "dialogue.wav"), "mp3": str(output / "dialogue.mp3"), "raw_wav": str(output / "model-raw.wav"), "transcript": str(output / "dialogue.transcript.txt")},
                "paid_api_calls": 0, "cache_hit": False,
            }
            # A complete raw result remains reusable even when the requested
            # audition duration bound is not met; retrying must not re-run GPU.
            if duration_ok:
                _atomic_bytes(job_dir / "dialogue.metrics.json", (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode())
                _atomic_bytes(job_dir / "dialogue.review.json", (json.dumps({"ok": True, "review_status": "pending_human_audition", "raw_pcm_preserved": True, "speech_rate_changed": False}, ensure_ascii=False, indent=2) + "\n").encode())
            else:
                report["status"] = "duration_out_of_bounds"
                _atomic_bytes(job_dir / "dialogue.metrics.json", (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode())
                _atomic_bytes(job_dir / "dialogue.review.json", (json.dumps({"ok": False, "review_status": "duration_out_of_bounds", "duration_seconds": duration, "min_seconds": min_seconds, "max_seconds": max_seconds, "raw_pcm_preserved": True, "speech_rate_changed": False}, ensure_ascii=False, indent=2) + "\n").encode())
            for source_file in job_dir.iterdir():
                if source_file.is_file():
                    shutil.copy2(source_file, output / source_file.name)
            return report
        except BaseException as exc:
            return _failure_report(output, exc, script=script, job_id=job_id, reference=reference, job_dir=job_dir)
    finally:
        for lock in reversed(acquired):
            lock.release()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline local SoulX two-host audition")
    parser.add_argument("--script", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--coordination-lock", type=Path)
    parser.add_argument("--bulletin", action="store_true")
    parser.add_argument("--min-seconds", type=float, default=300)
    parser.add_argument("--max-seconds", type=float, default=600)
    args = parser.parse_args(argv)
    print("[soulx-stage] python_started", flush=True)
    try:
        report = run(args)
    except BaseException as exc:
        output = Path(args.output_dir).resolve()
        is_production = any(
            output == production_root.resolve() or production_root.resolve() in output.parents
            for production_root in KNOWN_PRODUCTION_OUTPUT_ROOTS
        )
        if not is_production:
            output.mkdir(parents=True, exist_ok=True)
            report = _failure_report(output, exc)
        else:
            report = {"ok": False, "status": "rejected", "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
