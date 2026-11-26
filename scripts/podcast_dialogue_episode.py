"""Build one private, review-gated dual-host podcast bundle.

This module is an orchestration seam for the daily worker.  It deliberately
does not publish, acquire the worker lock, call an API, or invoke the model
in-process: the writer and SoulX backend each run exactly once as child
processes, and only a fully validated candidate is copied into ``stage``.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import date, datetime, timezone
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Mapping


class DialogueBundleError(ValueError):
    """A closed-gate failure; no candidate is safe to stage."""


CommandRunner = Callable[..., Any]
DurationProbe = Callable[[Path], float]


_BLOCKED_FINISH_REASONS = frozenset({
    "length", "max_tokens", "content_filter", "content-filter", "filter", "error", "truncated",
})
_VOICE_REFERENCE = {"S1": "male_1", "S2": "female_1"}
_PROTECTED_OUTPUT_ROOTS = tuple(
    Path(item).expanduser().resolve()
    for item in (
        "/Users/bruce/Coding/NewsRadar/TrendRadar/output",
        "/Users/bruce/Coding/NewsRadar-production/TrendRadar/output",
    )
)


def _json_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                        sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DialogueBundleError(f"cannot read {label}: {path}: {exc}") from exc


def _ensure_private_root(path: Path) -> Path:
    """Use the writer's guard when available, with a local fail-closed guard."""
    candidate = path.expanduser().resolve()
    try:
        # Delayed import keeps importing this helper independent of edge/TTS
        # packages in unit-test and worker startup environments.
        from generate_dialogue_podcast import ensure_private_output_root
    except Exception:
        ensure_private_output_root = None
    if ensure_private_output_root is not None:
        try:
            candidate = ensure_private_output_root(candidate)
        except Exception as exc:
            raise DialogueBundleError(str(exc)) from exc
    if any(candidate == root or candidate.is_relative_to(root)
           for root in _PROTECTED_OUTPUT_ROOTS):
        raise DialogueBundleError(
            f"refusing dialogue private root inside production output: {candidate}"
        )
    return candidate


def _freeze_materials(day_root: Path, materials: Mapping[str, Any], report_date: date) -> tuple[Path, str]:
    if not isinstance(materials, Mapping):
        raise DialogueBundleError("materials must be a mapping")
    if materials.get("date") != report_date.isoformat():
        raise DialogueBundleError("materials date does not match report date")
    input_path = day_root / "input.json"
    incoming_hash = _json_hash(materials)
    if input_path.exists():
        frozen = _read_json(input_path, "frozen materials")
        frozen_hash = _json_hash(frozen)
        if frozen_hash != incoming_hash:
            raise DialogueBundleError("frozen materials changed; refusing a new dialogue edition")
        return input_path, frozen_hash
    _write_json(input_path, materials)
    return input_path, incoming_hash


def _run(command_runner: CommandRunner, command: list[str], *, timeout: float,
         state_path: Path | None = None) -> Any:
    # A per-child durable state distinguishes model writing, synthesis and
    # timeout. Logs remain local; no API credentials or source bodies enter
    # state metadata. A failed invocation is never automatically repeated.
    started = time.monotonic()
    state = {"stage": Path(command[1]).stem, "status": "running",
             "started_at": datetime.now(timezone.utc).isoformat(),
             "timeout_seconds": timeout}
    if state_path is not None:
        _write_json(state_path, state)
    try:
        if state_path is None:
            result = command_runner(command, check=True, timeout=timeout)
        else:
            log_path = state_path.with_suffix(".log")
            with log_path.open("a", encoding="utf-8") as log:
                result = command_runner(command, check=True, timeout=timeout,
                                        stdout=log, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL)
        state["status"] = "completed"
        return result
    except BaseException as exc:
        state["status"] = "timed_out" if isinstance(exc, subprocess.TimeoutExpired) else "failed"
        state["error_type"] = type(exc).__name__
        raise
    finally:
        state["elapsed_seconds"] = round(time.monotonic() - started, 3)
        state["finished_at"] = datetime.now(timezone.utc).isoformat()
        if state_path is not None:
            _write_json(state_path, state)


def _load_dialogue_modules() -> tuple[Any, Any]:
    """Load pure dialogue helpers only after the writer subprocess completes."""
    try:
        import podcast_dialogue
    except ImportError as exc:
        raise DialogueBundleError(f"dialogue validator unavailable: {exc}") from exc
    return podcast_dialogue, podcast_dialogue


def _finish_reason(review: Mapping[str, Any]) -> str | None:
    metadata = review.get("response_metadata")
    if not isinstance(metadata, Mapping):
        metadata = {}
    value = metadata.get("finish_reason", review.get("finish_reason"))
    if value is None:
        return None
    return str(value).strip().lower()


def _validate_script(day_root: Path, report_date: date) -> tuple[dict[str, Any], dict[str, Any], str]:
    dialogue_module, _ = _load_dialogue_modules()
    script_path = day_root / "dialogue-draft.json"
    plan_path = day_root / "writer-plan.json"
    review_path = day_root / "draft-review.json"
    review = _read_json(review_path, "writer review")
    if not isinstance(review, Mapping):
        raise DialogueBundleError("writer review is not an object")
    reason = _finish_reason(review)
    if reason in _BLOCKED_FINISH_REASONS:
        raise DialogueBundleError(f"writer response was truncated or blocked: {reason}")
    if any(review.get(key) in {"invalid_model_output", "generation_failed"}
           for key in ("mode", "status")):
        raise DialogueBundleError("writer review rejected the dialogue")
    warnings = review.get("warnings")
    if warnings:
        raise DialogueBundleError("writer review contains warnings; human review is required")
    if not script_path.is_file() or not plan_path.is_file():
        raise DialogueBundleError("writer did not produce dialogue-draft.json and writer-plan.json")
    dialogue = _read_json(script_path, "dialogue draft")
    writer_plan = _read_json(plan_path, "writer plan")
    if not isinstance(writer_plan, Mapping):
        raise DialogueBundleError("writer plan is not an object")
    try:
        projected_plan, source_text = dialogue_module.project_editorial_plan(writer_plan)
        # Keep the local variable intentionally referenced: this call is the
        # single approved projection boundary for numeric/date provenance.
        del projected_plan
        normalized, report = dialogue_module.validate_dialogue(
            dialogue, source_text=source_text, enforce_length=True
        )
    except Exception as exc:
        raise DialogueBundleError(f"dialogue validation failed: {exc}") from exc
    if report.get("warnings"):
        raise DialogueBundleError("dialogue has warnings requiring human review")
    if report.get("introduced_numeric_tokens"):
        raise DialogueBundleError("dialogue introduced numeric tokens outside the writer plan")
    if normalized.get("hosts") != dialogue_module.HOSTS:
        raise DialogueBundleError("dialogue hosts do not match the fixed voice contract")
    if review.get("date") != report_date.isoformat():
        raise DialogueBundleError("writer review date does not match report date")
    return normalized, dict(report), hashlib.sha256(script_path.read_bytes()).hexdigest()


def _measure_duration(path: Path, duration_probe: DurationProbe | None) -> float:
    if duration_probe is not None:
        value = duration_probe(path)
    else:
        try:
            # Delayed import avoids importing edge_tts while merely importing
            # the daily worker or its tests.
            from generate_daily_podcast import measure_duration
            value = measure_duration(path)
        except ImportError:
            probe = shutil.which("ffprobe")
            if not probe:
                raise DialogueBundleError("cannot measure MP3 duration: ffprobe is unavailable")
            result = subprocess.run(
                [probe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "json", str(path)], check=True, capture_output=True,
                text=True, timeout=30,
            )
            value = json.loads(result.stdout)["format"]["duration"]
    try:
        duration = float(value)
    except (TypeError, ValueError) as exc:
        raise DialogueBundleError("MP3 duration probe did not return a number") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise DialogueBundleError("MP3 duration probe returned an invalid duration")
    return duration


def _validate_metrics(metrics: Mapping[str, Any], dialogue: Mapping[str, Any],
                      script_sha: str, min_seconds: float,
                      max_seconds: float) -> list[dict[str, Any]]:
    if metrics.get("status") != "ok" or metrics.get("ok") is not True or metrics.get("duration_ok") is not True:
        raise DialogueBundleError("SoulX metrics are not an ok candidate")
    if metrics.get("script_sha256") != script_sha:
        raise DialogueBundleError("SoulX script_sha256 does not match the frozen script")
    try:
        reported = float(metrics.get("total_duration_seconds"))
    except (TypeError, ValueError) as exc:
        raise DialogueBundleError("SoulX metrics have no numeric duration") from exc
    if not math.isfinite(reported) or not min_seconds <= reported <= max_seconds:
        raise DialogueBundleError("SoulX reported duration is outside the configured range")
    reference = metrics.get("reference")
    if not isinstance(reference, Mapping) or reference.get("voices") != _VOICE_REFERENCE:
        raise DialogueBundleError("SoulX voice reference is not the fixed male_1/female_1 pair")
    turns = metrics.get("turns")
    expected_turns = dialogue.get("text")
    if not isinstance(expected_turns, list) or not expected_turns:
        raise DialogueBundleError("validated dialogue has no turns")
    if metrics.get("turn_count") != len(expected_turns) or not isinstance(turns, list) or len(turns) != len(expected_turns):
        raise DialogueBundleError("SoulX turn telemetry is incomplete")
    for index, (row, expected) in enumerate(zip(turns, expected_turns)):
        if not isinstance(row, Mapping) or not isinstance(expected, (list, tuple)) or len(expected) != 2:
            raise DialogueBundleError(f"SoulX turn {index} telemetry is invalid")
        speaker = expected[0]
        if (row.get("index") != index or row.get("speaker") != speaker
                or row.get("voice") != _VOICE_REFERENCE.get(speaker)):
            raise DialogueBundleError(f"SoulX turn {index} does not match the dialogue script")
    if not turns:
        raise DialogueBundleError("SoulX metrics do not contain turn telemetry")
    return turns


def _chapters(turns: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], float]:
    elapsed = 0.0
    result: list[dict[str, Any]] = []
    previous: str | None = None
    for index, turn in enumerate(turns):
        if not isinstance(turn, Mapping):
            raise DialogueBundleError(f"SoulX turn {index} telemetry is invalid")
        try:
            pause = float(turn.get("pause_before_ms", 0)) / 1000.0
            raw = float(turn.get("raw_audio_seconds"))
        except (TypeError, ValueError) as exc:
            raise DialogueBundleError(f"SoulX turn {index} duration telemetry is invalid") from exc
        if not math.isfinite(pause) or not math.isfinite(raw) or pause < 0 or raw <= 0:
            raise DialogueBundleError(f"SoulX turn {index} duration telemetry is invalid")
        start = elapsed + pause
        topic = str(turn.get("topic") or "").strip()
        if topic and topic != previous:
            # Transport labels belong to the writer/backend, while the
            # published chapter buttons use ordinary programme labels.
            label = {"overview": "今日概览", "discussion": "重点讨论",
                     "closing": "结束语"}.get(topic, topic)
            result.append({"start_seconds": round(start, 3), "title": label})
            previous = topic
        elapsed = start + raw
    return result, elapsed


def _public_sources(materials: Mapping[str, Any], report_date: date) -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    for item in materials.get("items", []):
        if not isinstance(item, Mapping):
            continue
        record = {key: item[key] for key in ("id", "title", "source", "url", "published_at", "source_genre")
                  if key in item}
        sources.append(record)
    return {"date": report_date.isoformat(), "sources": sources}


def build_dialogue_bundle(
    *,
    materials: dict,
    report_date: date,
    prompt_path: Path,
    runtime_root: Path,
    stage: Path,
    private_root: Path | None = None,
    min_seconds: float = 300,
    max_seconds: float = 600,
    command_runner: CommandRunner = subprocess.run,
    duration_probe: DurationProbe | None = None,
    resilient: bool = False,
) -> dict[str, Any]:
    """Create a validated daily dialogue candidate without publishing it."""
    if not isinstance(report_date, date):
        raise DialogueBundleError("report_date must be a date")
    if float(min_seconds) < 0 or float(max_seconds) < float(min_seconds):
        raise DialogueBundleError("duration bounds are invalid")
    if not prompt_path.is_file():
        raise DialogueBundleError(f"prompt file is unavailable: {prompt_path}")
    stage = Path(stage).expanduser().resolve()
    stage.mkdir(parents=True, exist_ok=True)
    if any(stage.iterdir()):
        raise DialogueBundleError("stage must be an empty staging directory")
    runtime_root = Path(runtime_root).expanduser().resolve()
    root = _ensure_private_root(private_root or runtime_root / "cache" / "newsradar-dialogue-editions")
    day_root = root / report_date.isoformat()
    day_root.mkdir(parents=True, exist_ok=True)
    input_path, material_hash = _freeze_materials(day_root, materials, report_date)

    writer = Path(__file__).with_name("generate_dialogue_podcast.py")
    writer_command = [
        sys.executable, str(writer), "--materials", str(input_path),
        "--date", report_date.isoformat(), "--output-root", str(root),
        "--prompt", str(prompt_path), "--planned", "--format", "slot-lines" if resilient else "plain-turns",
        "--draft-only",
    ]
    script_path = day_root / "dialogue-draft.json"
    bulletin = False
    if resilient:
        from podcast_dialogue import DialogueError, assess_materials, project_editorial_plan
        from podcast_episode_policy import BULLETIN_MIN_SECONDS, bulletin_dialogue, writing_seconds
        from trendradar.ai.daily_edition import load_edition, podcast_payload

        if not materials.get("shared_edition"):
            raise DialogueBundleError("resilient generation requires a frozen shared edition")
        edition = load_edition(Path(materials["shared_edition_path"]))
        if podcast_payload(edition) != materials:
            raise DialogueBundleError("materials differ from frozen edition")
        if not assess_materials(materials, report_date)["adequate"]:
            raise DialogueBundleError("frozen materials insufficient")
        try:
            bulletin_script, bulletin_review = bulletin_dialogue(
                *project_editorial_plan(materials["shared_editorial_plan"]))
            bulletin_path = day_root / "bulletin-draft.json"
            _write_json(bulletin_path, bulletin_script)
            bulletin_sha = hashlib.sha256(bulletin_path.read_bytes()).hexdigest()
            _write_json(day_root / "bulletin-review.json", {
                **bulletin_review, "date": report_date.isoformat(), "status": "ready",
                "script_sha256": bulletin_sha, "material_sha256": material_hash,
            })
            bulletin_error = None
        except DialogueError as error:
            bulletin_error = error
            (day_root / "bulletin-review.json").unlink(missing_ok=True)

    try:
        remaining = writing_seconds(report_date) if resilient else 1800
        if remaining < 1:
            raise DialogueBundleError("writing deadline reached")
        _run(command_runner, writer_command, timeout=remaining, state_path=day_root / "writer-process.json")
        if resilient and writing_seconds(report_date) < 1:
            raise DialogueBundleError("writing deadline reached")
        dialogue, dialogue_review, script_sha = _validate_script(day_root, report_date)
    except (subprocess.SubprocessError, DialogueBundleError) as failure:
        if not resilient:
            raise
        if bulletin_error is not None:
            raise DialogueBundleError(f"writer and bulletin rejected: {bulletin_error}") from failure
        dialogue, dialogue_review, script_sha = bulletin_script, bulletin_review, bulletin_sha
        script_path = bulletin_path
        _write_json(day_root / "bulletin-review.json", {
            **bulletin_review, "date": report_date.isoformat(), "status": "selected",
            "writer_failure": str(failure), "script_sha256": script_sha,
            "material_sha256": material_hash,
        })
        bulletin = True
        min_seconds = BULLETIN_MIN_SECONDS

    trial_python = runtime_root / ".venv" / "bin" / "python"
    backend = Path(__file__).with_name("soulx_dialogue_synthesize.py")
    synthesis_dir = day_root / "synthesis"
    backend_command = [
        str(trial_python), str(backend), "--script", str(script_path),
        "--output-dir", str(synthesis_dir), "--runtime-root", str(runtime_root),
        "--min-seconds", str(float(min_seconds)), "--max-seconds", str(float(max_seconds)),
    ]
    if bulletin:
        backend_command.append("--bulletin")
    _run(command_runner, backend_command, timeout=5400, state_path=day_root / "synthesis-process.json")
    metrics_path = synthesis_dir / "dialogue.metrics.json"
    metrics = _read_json(metrics_path, "SoulX metrics")
    if not isinstance(metrics, Mapping):
        raise DialogueBundleError("SoulX metrics are not an object")
    turns = _validate_metrics(metrics, dialogue, script_sha, float(min_seconds), float(max_seconds))
    audio = synthesis_dir / "dialogue.mp3"
    if not audio.is_file() or audio.stat().st_size <= 0:
        raise DialogueBundleError("SoulX produced no complete MP3")
    duration = _measure_duration(audio, duration_probe)
    if not float(min_seconds) <= duration <= float(max_seconds):
        raise DialogueBundleError("measured MP3 duration is outside the configured range")
    chapters, telemetry_duration = _chapters(turns)
    try:
        reported_duration = float(metrics["total_duration_seconds"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DialogueBundleError("SoulX metrics have no numeric duration") from exc
    if (abs(duration - reported_duration) > 0.15
            or abs(duration - telemetry_duration) > 0.15):
        raise DialogueBundleError(
            "measured MP3 duration disagrees with SoulX metrics or raw-audio telemetry"
        )

    dialogue_module, _ = _load_dialogue_modules()
    transcript = dialogue_module.transcript_text(dialogue)
    if any(marker in transcript for marker in ("<|", "[S1]", "[S2]", "[SECTION:")):
        raise DialogueBundleError("public transcript contains a control or transport marker")
    (stage / "daily.mp3").write_bytes(audio.read_bytes())
    (stage / "daily.txt").write_text(transcript, encoding="utf-8")
    _write_json(stage / "sources.json", _public_sources(materials, report_date))
    _write_json(stage / "narration-review.json", {
        "version": dialogue_review.get("version", "dual-dialogue-v1"),
        "date": report_date.isoformat(),
        "turn_count": dialogue_review.get("turn_count"),
        "spoken_chars": dialogue_review.get("spoken_chars"),
        "script_sha256": script_sha,
        "material_sha256": material_hash,
        "warnings": [],
        "edition_variant": "bulletin" if bulletin else "dialogue",
        "sample_limit": 0,
        "listening_review_required": True,
        "review_status": "pending_human_audition",
    })
    wall = metrics.get("generation_wall_seconds")
    try:
        wall_value = float(wall) if wall is not None else None
    except (TypeError, ValueError):
        wall_value = None
    synthesis_summary = {
        "device": metrics.get("device"),
        "wall_seconds": wall_value,
        "rtf": (wall_value / duration if wall_value is not None and duration > 0 else None),
    }
    return {
        "duration_seconds": duration,
        "script_chars": dialogue_review.get("spoken_chars"),
        "engine": "soulx",
        "format": "daily_dialogue",
        "edition_variant": "bulletin" if bulletin else "dialogue",
        "voice": "male_1+female_1",
        "voice_label": "SoulX · 知远/晓宁",
        "rate": "model_default",
        "volume": "model_default",
        "chapters": chapters,
        "synthesis": synthesis_summary,
        "pending_human_audition": True,
        "material_sha256": material_hash,
        "script_sha256": script_sha,
        **({"shared_edition": materials["shared_edition"], "news_cutoff_at": materials["shared_edition"]["news_cutoff_at"]} if materials.get("shared_edition") else {}),
    }


__all__ = ["DialogueBundleError", "build_dialogue_bundle"]
