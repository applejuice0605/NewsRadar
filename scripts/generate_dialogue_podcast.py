#!/usr/bin/env python3
"""Create a private, reviewable NewsRadar dual-host dialogue draft.

The slot pipeline permits one separately cached, targeted revision.  Audio synthesis is kept
behind the separate SoulX dialogue runner; ``--draft-only`` is the review-safe
mode for creating a candidate without touching GPU/audio state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import importlib.util
from datetime import date
from pathlib import Path
from typing import Any, Mapping


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
TREND_RADAR = REPO_ROOT / "TrendRadar"
for _path in (SCRIPT_DIR, REPO_ROOT, TREND_RADAR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from podcast_dialogue import (  # noqa: E402
    DialogueError,
    assess_materials,
    build_fact_brief,
    one_call_draft,
    normalize_turn_transport,
    plain_turn_rows,
    _json_candidate,
    parse_and_validate,
    project_editorial_plan,
    transcript_text,
    validate_dialogue,
    write_json_atomic,
)


def normalize_final_signoff_section(raw: str) -> tuple[str, bool]:
    """Restore a missing transport marker only at an explicit final goodbye."""
    sections = re.findall(r"(?m)^\s*\[SECTION:([^\]]+)\]\s*$", raw)
    if sections != ["overview", "discussion"]:
        return raw, False
    lines = raw.splitlines()
    populated = [index for index, line in enumerate(lines) if line.strip()]
    if not populated:
        return raw, False
    index = populated[-1]
    if not re.fullmatch(
            r"\s*\[\d{2,3}:S[12]\]\s*(?:好[，,。]?\s*)?今天就聊到这里[^\n]*再见[。！!]?\s*",
            lines[index]):
        return raw, False
    lines.insert(index, "[SECTION:closing]")
    return "\n".join(lines), True


def normalize_long_semicolon_sentences(raw: str) -> tuple[str, list[dict[str, str]]]:
    """Plan breath boundaries without rewriting a word, number or voice tag.

    Only existing Chinese commas/semicolons outside balanced quotes may become
    periods. Unsafe, indivisible or unbalanced text stays subject to rejection.
    The historical function name remains compatible with existing callers.
    """
    from podcast_dialogue import _spoken_sentence_units
    changes = []
    opening = {'“': '”', '「': '」', '『': '』', '（': '）', '(': ')'}
    def normalize_line(line):
        match = re.match(r'^(\s*\[\d{2,3}:S[12]\]\s*)(.*)$', line)
        if not match:
            return line
        prefix, body = match.groups()
        stack = []
        boundaries = []
        for i, char in enumerate(body):
            if char == '"':
                if stack and stack[-1] == '"': stack.pop()
                else: stack.append('"')
            elif char in opening: stack.append(opening[char])
            elif char in ('”', '」', '』', '）', ')'):
                if not stack or stack.pop() != char: return line
            elif not stack and char in '，；':
                if not (i > 0 and i + 1 < len(body) and body[i-1].isdigit() and body[i+1].isdigit()):
                    boundaries.append(i)
        if stack: return line
        # All accepted edits are punctuation substitutions, so indices stay fixed.
        output = list(body)
        def split_segment(start, end):
            cursor = start
            while _spoken_sentence_units(body[cursor:end]) > 65:
                candidates = [i for i in boundaries if cursor < i < end
                    and 12 <= _spoken_sentence_units(body[cursor:i]) <= 55]
                if not candidates: break
                # Prefer a semicolon, then the last usable clause boundary.
                semicolons = [i for i in candidates if body[i] == '；']
                index = (semicolons or candidates)[-1]
                output[index] = '。'
                cursor = index + 1
        start = 0
        stack = []
        for i, char in enumerate(body):
            if char == '"':
                if stack and stack[-1] == '"': stack.pop()
                else: stack.append('"')
            elif char in opening: stack.append(opening[char])
            elif stack and char == stack[-1]: stack.pop()
            if not stack and char in '。！？!?：':
                split_segment(start, i)
                start = i + 1
        split_segment(start, len(body))
        changed = ''.join(output)
        if changed != body:
            # Machine-enforced conservation: nothing but existing punctuation
            # can change, even if this implementation is later edited.
            assert re.sub('[，；。]', '', body) == re.sub('[，；。]', '', changed)
            changes.append({'kind': 'breath_boundary_v2', 'before': body, 'after': changed})
        return prefix + changed
    return '\n'.join(normalize_line(line) for line in raw.split('\n')), changes


def normalize_turns_json_sentences(raw: str) -> tuple[str, list[dict[str, str]]]:
    """Apply the same punctuation-only breath pass to turns-json text rows."""
    try:
        value = json.loads(raw.strip())
    except (TypeError, json.JSONDecodeError):
        return raw, []
    if not isinstance(value, dict) or set(value) != {"text"} or not isinstance(value["text"], list):
        return raw, []
    if value['text'] and all(isinstance(item, str) for item in value['text']):
        if len(value['text']) % 2:
            return raw, []
        value['text'] = [value['text'][index:index + 2] for index in range(0, len(value['text']), 2)]
    changes: list[dict[str, str]] = []
    for index, turn in enumerate(value["text"]):
        if not isinstance(turn, list) or len(turn) != 2 or turn[0] not in {"S1", "S2"}:
            continue
        body = turn[1]
        if not isinstance(body, str):
            continue
        prefix = f"[{index + 1:02d}:{turn[0]}] "
        normalized, row_changes = normalize_long_semicolon_sentences(prefix + body)
        if normalized.startswith(prefix):
            normalized_body = normalized[len(prefix):]
            if normalized_body != body:
                turn[1] = normalized_body
                changes.extend(row_changes)
    if not changes:
        return raw, changes
    return json.dumps(value, ensure_ascii=False), changes


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--materials", required=True, type=Path,
                        help="dated podcast materials JSON")
    parser.add_argument("--date", required=True, help="report date YYYY-MM-DD")
    parser.add_argument("--output-root", type=Path,
                        default=REPO_ROOT / "output" / "dialogue",
                        help="private draft/output root")
    parser.add_argument("--prompt", required=True, type=Path,
                        help="writer prompt supplied by the caller")
    parser.add_argument("--draft-only", action="store_true",
                        help="write private draft/review files and do not synthesize")
    parser.add_argument("--format", choices=("json", "speaker-lines", "turns-json", "plain-turns", "slot-lines"), default="json",
                        help="writer transport format; raw response is always retained")
    parser.add_argument("--cache-only", action="store_true",
                        help="replay a saved writer response; refuse any new model call")
    parser.add_argument("--planned", action="store_true",
                        help="extract a compact editorial plan locally before the one writing call")
    parser.add_argument("--reviewed-script", type=Path,
                        help="human-reviewed dialogue JSON; skips the writing call")
    parser.add_argument("--reviewed-script-sha256",
                        help="sha256 of the reviewed JSON file or canonical dialogue")
    parser.add_argument("--trialpython", type=Path,
                        help="SoulX trial Python for the future synthesis hand-off")
    parser.add_argument("--runtime-root", type=Path,
                        help="SoulX runtime root; otherwise derive from --trialpython")
    parser.add_argument("--coordination-lock", type=Path,
                        help="optional shared SoulX/GPU lock; default lets backend use its cache lock")
    return parser.parse_args(argv)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DialogueError(f"cannot read JSON: {path}: {exc}") from exc


PROTECTED_OUTPUT_ROOTS = tuple(
    Path(path).expanduser().resolve()
    for path in (
        "/Users/bruce/Coding/NewsRadar/TrendRadar/output",
        "/Users/bruce/Coding/NewsRadar-production/TrendRadar/output",
    )
)


def ensure_private_output_root(path: Path) -> Path:
    """Reject all paths inside either production TrendRadar output tree."""
    candidate = path.expanduser().resolve()
    if any(candidate == root or candidate.is_relative_to(root)
           for root in PROTECTED_OUTPUT_ROOTS):
        raise DialogueError(
            f"refusing dialogue output inside protected production tree: {candidate}"
        )
    return candidate


def _load_ai_config() -> Mapping[str, Any]:
    """Reuse the existing podcast configuration without changing it."""
    try:
        import generate_daily_podcast as existing
        _podcast, config = existing.load_podcast_config()
        return config
    except Exception as exc:  # imports are intentionally delayed for reviewed drafts
        raise DialogueError(f"existing AI configuration is unavailable: {exc}") from exc


def _client_factory(config: Mapping[str, Any]) -> Any:
    from trendradar.ai.client import AIClient
    return AIClient(dict(config))


def _source_audit_text(payload: Mapping[str, Any]) -> str:
    # Titles stay local for numeric provenance checks only.  They are never
    # included in build_fact_brief/model input.
    parts: list[str] = [str(payload.get("date") or "")]
    for item in payload.get("items", []):
        if isinstance(item, dict):
            parts.extend([str(item.get("title") or ""),
                          str(item.get("published_at") or ""),
                          str(item.get("text") or "")])
    return "\n".join(parts)


def _reviewed_hashes(path: Path, dialogue: Mapping[str, Any]) -> set[str]:
    raw_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    canonical = json.dumps(dialogue, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    return {raw_hash, hashlib.sha256(canonical).hexdigest()}


def _future_backend(dialogue: Mapping[str, Any], output_root: Path,
                    trialpython: Path | None, day_root: Path | None = None,
                    runtime_root: Path | None = None,
                    coordination_lock: Path | None = None) -> None:
    """Invoke the bounded SoulX runner once its sibling script is present."""
    backend = SCRIPT_DIR / "soulx_dialogue_synthesize.py"
    if not backend.is_file():
        raise DialogueError(
            "SoulX dialogue backend is not installed yet; use --draft-only until "
            "scripts/soulx_dialogue_synthesize.py provides its CLI contract"
        )
    if trialpython is None:
        configured = os.environ.get("NEWSRADAR_SOULX_PYTHON")
        trialpython = Path(configured).expanduser() if configured else None
    if trialpython is None or not trialpython.is_file():
        raise DialogueError("non-draft mode requires --trialpython (or NEWSRADAR_SOULX_PYTHON)")
    day_root = day_root or output_root
    script_path = day_root / "dialogue-draft.json"
    output_dir = day_root / "synthesis"
    # Do not resolve a venv symlink to Homebrew's interpreter: the runtime
    # root is the trial checkout containing source/models/cache.
    runtime_root = (runtime_root.expanduser().absolute() if runtime_root
                    else trialpython.expanduser().absolute().parent.parent)
    command = [
        str(trialpython), str(backend),
        "--script", str(script_path), "--output-dir", str(output_dir),
        "--runtime-root", str(runtime_root),
        "--min-seconds", "300", "--max-seconds", "600",
    ]
    if coordination_lock is not None:
        command.extend(["--coordination-lock", str(coordination_lock.expanduser().absolute())])
    subprocess.run(command, check=True, timeout=5400)


def _validate_existing_materials(payload: Any, report_date: date) -> None:
    """Run the established dated/source/URL/body gate before any AI call."""
    try:
        spec = importlib.util.spec_from_file_location(
            "newsradar_existing_daily_podcast", SCRIPT_DIR / "generate_daily_podcast.py"
        )
        if spec is None or spec.loader is None:
            raise DialogueError("existing material validator is unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.validate_editorial_materials(payload, report_date)
    except DialogueError:
        raise
    except ModuleNotFoundError:
        # Unit-test environments may not install the production TTS package.
        # Use the exact validator used by that helper without importing audio.
        material_spec = importlib.util.spec_from_file_location(
            "newsradar_podcast_materials", SCRIPT_DIR / "podcast_materials.py"
        )
        if material_spec is None or material_spec.loader is None:
            raise DialogueError("existing material validator is unavailable")
        material_module = importlib.util.module_from_spec(material_spec)
        material_spec.loader.exec_module(material_module)
        ok, errors = material_module.validate_payload(payload, report_date)
        if not ok:
            raise DialogueError("invalid editorial materials: " + ", ".join(errors))
    except Exception as exc:
        raise DialogueError(f"invalid editorial materials: {exc}") from exc


def generate(args: argparse.Namespace) -> dict[str, Any]:
    try:
        report_date = date.fromisoformat(args.date)
    except ValueError as exc:
        raise DialogueError("--date must use YYYY-MM-DD") from exc
    if report_date.isoformat() != args.date:
        raise DialogueError("--date must use YYYY-MM-DD")
    if not args.materials.is_file():
        raise DialogueError(f"materials file is unavailable: {args.materials}")
    if not args.prompt.is_file():
        raise DialogueError(f"prompt file is unavailable: {args.prompt}")

    output_root = ensure_private_output_root(args.output_root)
    payload = _load_json(args.materials)
    if payload.get('shared_edition'):
        from trendradar.ai.daily_edition import load_edition,podcast_payload
        saved=load_edition(Path(payload['shared_edition_path']))
        if podcast_payload(saved)!=payload:
            raise DialogueError('shared factual edition payload differs from frozen artifact')
    _validate_existing_materials(payload, report_date)
    adequacy = assess_materials(payload, report_date)
    day_root = output_root / report_date.isoformat()
    day_root.mkdir(parents=True, exist_ok=True)
    review_path = day_root / "draft-review.json"
    if not adequacy.get("adequate"):
        report = {"version": "dual-dialogue-review-v1", "date": args.date,
                  "mode": "insufficient_materials", "material_adequacy": adequacy,
                  "additional_model_calls": 0, "listening_review_required": True}
        write_json_atomic(review_path, report)
        raise DialogueError("insufficient source material; no writing call made")

    brief, _ = build_fact_brief(payload, report_date)
    prompt = args.prompt.read_text(encoding="utf-8")
    source_text = _source_audit_text(payload)
    if getattr(args, "planned", False):
        from podcast_editorial_plan import build_editorial_plan
        plan = payload.get("shared_editorial_plan") or build_editorial_plan(payload)
        write_json_atomic(day_root / "editorial-plan.json", plan)
        writer_plan, source_text = project_editorial_plan(plan)
        write_json_atomic(day_root / "writer-plan.json", writer_plan)
        brief = json.dumps(writer_plan, ensure_ascii=False, sort_keys=True)
    if getattr(args, "format", "") == "slot-lines":
        from podcast_episode_policy import slot_instruction
        prompt += "\n" + slot_instruction(len(writer_plan["topics"]) if getattr(args, "planned", False) else 1)
    call_report: dict[str, Any] = {"additional_model_calls": 0, "cache_hit": False}
    if args.reviewed_script:
        raw = args.reviewed_script.read_text(encoding="utf-8")
        reviewed_value = _load_json(args.reviewed_script)
        if args.reviewed_script_sha256 and args.reviewed_script_sha256 not in _reviewed_hashes(
                args.reviewed_script, reviewed_value):
            raise DialogueError("--reviewed-script-sha256 does not match the reviewed JSON")
        dialogue, dialogue_report = validate_dialogue(
            reviewed_value, source_text=source_text, enforce_length=not args.draft_only
        )
        call_report.update({"mode": "reviewed_script", "reviewed_script": str(args.reviewed_script),
                            "reviewed_script_sha256": hashlib.sha256(raw.encode()).hexdigest()})
    else:
        ai_config = _load_ai_config()
        raw, call_report = one_call_draft(
            brief=brief, prompt=prompt,
            cache_root=day_root / "private",
            ai_config=ai_config, client_factory=_client_factory,
            output_format=getattr(args, "format", "json"),
            cache_only=getattr(args, "cache_only", False),
        )
        # A cache replay must retain its historical transport and exact raw
        # bytes: changing section/punctuation normalization would alter the
        # canonical script and miss warm audio keyed by its old hash.
        effective_format = call_report.get('writer_output_format', getattr(args, 'format', 'json'))
        try:
            if call_report.get("response_metadata", {}).get("finish_reason") in {
                    "length", "max_tokens", "content_filter", "error"}:
                raise DialogueError("writer response was truncated or blocked; raw retained, no automatic retry")
            if effective_format == "slot-lines":
                from podcast_episode_policy import SLOT_COUNT, SLOT_TARGET, read_slots, slot_dialogue, writing_seconds
                from podcast_dialogue import MAX_CHARS, speech_char_count

                allowed = set(range(1, SLOT_COUNT + 1))
                rows = read_slots(raw, allowed)
                targets = allowed - rows.keys()
                if sum(speech_char_count(line) for line in rows.values()) > MAX_CHARS:
                    targets |= {slot for slot, line in rows.items() if speech_char_count(line) > SLOT_TARGET}
                if targets and writing_seconds(report_date) >= 60:
                    targets = set(targets)
                    repair_brief = json.dumps({
                        "frozen_facts": json.loads(brief) if getattr(args, "planned", False) else brief,
                        "slots": {f"{slot:02d}": rows.get(slot, "") for slot in sorted(targets)},
                    }, ensure_ascii=False, sort_keys=True)
                    repair_prompt = (
                        "只修订下列指定轮次。空白轮次补充有依据的台词，超长轮次压缩。"
                        "每轮目标约60字符。只输出指定编号及台词，例如 07: 台词。"
                        "不输出未指定的轮次、说话人或说明。仅使用冻结事实。\n{{FACT_BRIEF}}")
                    revision, revision_audit = one_call_draft(
                        brief=repair_brief, prompt=repair_prompt, cache_root=day_root / "revision",
                        ai_config=ai_config, client_factory=_client_factory,
                        output_format="slot-lines", cache_only=getattr(args, "cache_only", False))
                    call_report["revision"] = {"slots": sorted(targets), **revision_audit}
                    call_report["additional_model_calls"] += revision_audit["additional_model_calls"]
                    if revision_audit.get("response_metadata", {}).get("finish_reason") in {
                            "length", "max_tokens", "content_filter", "error"}:
                        raise DialogueError("revision response incomplete")
                    replacements = read_slots(revision, targets)
                    if set(replacements) != targets:
                        raise DialogueError("revision must return every requested slot")
                    rows.update(replacements)
                raw = json.dumps({"text": slot_dialogue(rows)["text"]}, ensure_ascii=False)
                effective_format = "turns-json"
            if effective_format == 'turns-json':
                if raw.lstrip().startswith("```"):
                    raw = json.dumps(_json_candidate(raw), ensure_ascii=False)
                raw, brackets_recovered = normalize_turn_transport(raw)
                call_report['array_brackets_recovered'] = brackets_recovered
                if brackets_recovered:
                    write_json_atomic(day_root / 'transport-normalization.json', {
                        'original_raw_sha256': call_report.get('raw_sha256'),
                        'normalized_sha256': hashlib.sha256(raw.encode()).hexdigest(),
                        'operation': 'turn_structure_recovery', 'strings_preserved_in_order': True,
                        'words_or_numbers_changed': False})
            if effective_format == "plain-turns":
                # The existing punctuation pass recognizes numbered lines.
                # Add internal numbering only; the provider never emits it.
                raw = json.dumps({"text": plain_turn_rows(raw)}, ensure_ascii=False)
                effective_format = "turns-json"
            raw, signoff_restored = normalize_final_signoff_section(raw)
            call_report['final_signoff_section_restored'] = signoff_restored
            if signoff_restored:
                write_json_atomic(day_root / 'transport-normalization.json', {
                    'original_raw_sha256': call_report.get('raw_sha256'),
                    'normalized_sha256': hashlib.sha256(raw.encode()).hexdigest(),
                    'inserted_marker': '[SECTION:closing]',
                    'words_or_numbers_changed': False})
            if effective_format == "turns-json":
                raw, punctuation_changes = normalize_turns_json_sentences(raw)
                rows = _json_candidate(raw)
                rows = rows.get("text", []) if isinstance(rows, dict) else []
                rows = rows if isinstance(rows, list) else []
                bodies = rows[1::2] if rows and all(isinstance(row, str) for row in rows) else [
                    row[1] for row in rows if isinstance(row, list) and len(row) == 2]
                call_report['empty_turns_omitted'] = sum(isinstance(body, str) and not body.strip() for body in bodies)
            else:
                raw, punctuation_changes = normalize_long_semicolon_sentences(raw)
            call_report['punctuation_normalization_count'] = len(punctuation_changes)
            if punctuation_changes:
                write_json_atomic(day_root / 'punctuation-normalization.json', {
                    'original_raw_sha256': call_report.get('raw_sha256'),
                    'normalized_sha256': hashlib.sha256(raw.encode()).hexdigest(),
                    'changes': punctuation_changes, 'words_or_numbers_changed': False})
            dialogue, dialogue_report = parse_and_validate(
                raw, source_text=source_text, enforce_length=not args.draft_only,
                output_format=effective_format,
            )
        except DialogueError as exc:
            failure_report = {
                "version": "dual-dialogue-review-v1", "date": args.date,
                "mode": "invalid_model_output", "material_adequacy": adequacy,
                "error": str(exc), **call_report,
                "factual_truth_verified": False,
                "model_output_semantically_verified": False,
                "listening_review_required": True,
            }
            write_json_atomic(review_path, failure_report)
            raise

    dialogue_report.update({"date": args.date, "material_adequacy": adequacy,
                            "mode": call_report.get("mode", "model_draft"),
                            "cache_hit": call_report.get("cache_hit", False),
                            "additional_model_calls": call_report.get("additional_model_calls", 0),
                            "factual_truth_verified": False,
                            "model_output_semantically_verified": False})
    write_json_atomic(day_root / "dialogue-draft.json", dialogue)
    (day_root / "transcript.txt").write_text(transcript_text(dialogue), encoding="utf-8")
    write_json_atomic(review_path, {**dialogue_report, **call_report})
    if not args.draft_only:
        _future_backend(dialogue, args.output_root, args.trialpython, day_root,
                        args.runtime_root, args.coordination_lock)
    return {"output_root": str(day_root), "dialogue": dialogue,
            "review": dialogue_report, "call": call_report}


def main(argv: list[str] | None = None) -> int:
    try:
        result = generate(parse_args(argv))
        print(json.dumps({"output_root": result["output_root"],
                          "additional_model_calls": result["review"].get("additional_model_calls", 0)},
                         ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"[dialogue] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
