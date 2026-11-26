#!/usr/bin/env python3
"""Daily, non-overlapping podcast worker, independent of news publication."""
from __future__ import annotations
import argparse
import fcntl
import json
import os
import re
import tempfile
import subprocess
from datetime import datetime, date, time, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
import generate_daily_podcast as podcast


COMMUNITY_HOSTS = {
    'juejin.cn', 'v2ex.com', 'zhihu.com', 'weibo.com', 'x.com', 'twitter.com',
    'reddit.com', 'csdn.net', 'bilibili.com', 'youtube.com', 'douban.com',
    'segmentfault.com',
}

GENERATION_STAGES = {"preflight", "writer", "script_validation", "synthesis"}
PUBLICATION_STAGES = {"publication", "remote_verification"}


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _state_path(root: Path) -> Path:
    return root / ".daily-worker-state.json"


def _load_state(root: Path, day: str) -> dict:
    state = {"date": day, "failed_attempts": 0}
    previous = _read_json(_state_path(root))
    if previous.get("date") == day:
        state.update(previous)
    return state


def _save_state(state_path: Path, state: dict) -> None:
    podcast.write_json_atomic(state_path, state)


def _start_generation_audit(root: Path, day: str, args: argparse.Namespace) -> tuple[Path, dict]:
    """Create an append-only provenance receipt for each locked invocation."""
    started = datetime.now(timezone.utc)
    audit_root = root / ".daily-run-audit"
    audit_root.mkdir(parents=True, exist_ok=True)
    receipt = {
        "date": day,
        "started_at": started.isoformat(),
        "launchd_service": os.environ.get("NEWSRADAR_LAUNCHD_LABEL",
                                          os.environ.get("XPC_SERVICE_NAME", "")),
        "date_override": bool(args.date),
        "time_override": bool(args.now),
        "resume_after_fix": bool(args.resume_after_fix),
        "generation_status": "started",
    }
    path = audit_root / (day + "-" + started.strftime("%Y%m%dT%H%M%S.%fZ") + f"-{os.getpid()}.json")
    podcast.write_json_atomic(path, receipt)
    return path, receipt


def _finish_generation_audit(path: Path, receipt: dict, *, status: str,
                             metadata: dict | None = None,
                             error: BaseException | None = None,
                             code: int | None = None) -> None:
    updated = {**receipt, "generation_status": status,
               "finished_at": datetime.now(timezone.utc).isoformat()}
    if metadata and metadata.get("audio_url"):
        updated["audio_url"] = metadata["audio_url"]
        updated["edition_variant"] = metadata.get("edition_variant", "dialogue")
    if error is not None:
        updated["error_type"] = type(error).__name__
        updated["error_code"] = type(error).__name__
    if code is not None:
        updated["exit_code"] = int(code)
    podcast.write_json_atomic(path, updated)


def _set_failure(state_path: Path, state: dict, stage: str, error: BaseException,
                 *, retryable: bool, increment_generation: bool = False,
                 code: str | None = None) -> None:
    """Persist a redacted, stage-specific failure while retaining old fields."""
    state["last_failure"] = {
        "stage": stage,
        "at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "retryable": bool(retryable),
        "code": code or type(error).__name__,
    }
    if increment_generation:
        state["failed_attempts"] = int(state.get("failed_attempts", 0) or 0) + 1
    _save_state(state_path, state)


def _episode_bundle(root: Path, index: Path, metadata: dict, config: dict) -> bool:
    """Validate a completed bundle without allowing a corrupt one to regenerate."""
    if (not isinstance(metadata, dict)
            or metadata.get("date") != index.parent.name
            or not isinstance(metadata.get("audio_url"), str)):
        return False
    audio_url = metadata["audio_url"]
    if not audio_url.startswith("/audio/") or ".." in Path(audio_url).parts:
        return False
    relative = Path(audio_url.removeprefix("/audio/"))
    if relative.is_absolute():
        return False
    try:
        audio = (root / relative).resolve()
        if not audio.is_relative_to(root.resolve()):
            return False
    except (OSError, ValueError):
        return False
    if not (audio.is_file() and audio.with_suffix(".txt").is_file()
            and audio.with_suffix(".json").is_file()):
        return False
    settings = config.get("podcast", config)
    try:
        duration = podcast.measure_duration(audio)
        max_audio = float(settings.get("max_audio_seconds", 600) or 600)
        min_audio = max(0.0, float(settings.get("min_audio_seconds", 0) or 0))
        podcast.validate_duration(duration, max_seconds=max_audio)
    except (ValueError, KeyError, TypeError, OSError):
        return False
    # A short-but-valid historical bundle remains preserved; the caller may
    # choose not to republish it under a newer minimum duration.
    return True


def _failure_stage_from_private_state(config: dict, day: str) -> str | None:
    """Infer a child-process stage for old runs that have no worker state."""
    try:
        runtime = Path(os.environ.get(
            "NEWSRADAR_SOULX_ROOT",
            str((config.get("soulx") or {}).get("runtime_root", "")),
        )).expanduser()
        private = (Path(config["dialogue_private_root"]).expanduser()
                   if config.get("dialogue_private_root")
                   else runtime / "cache/newsradar-dialogue-editions")
        day_root = private / day
        synthesis = _read_json(day_root / "synthesis-process.json")
        if (_read_json(day_root / "bulletin-review.json").get("status") == "selected"
                and synthesis.get("status") in {"failed", "timed_out"}):
            return "synthesis"
        review = _read_json(day_root / "draft-review.json")
        if review.get("mode") == "invalid_model_output" or review.get("warnings"):
            return "script_validation"
        synthesis = _read_json(day_root / "synthesis-process.json")
        if synthesis.get("status") in {"failed", "timed_out"}:
            return "synthesis"
        writer = _read_json(day_root / "writer-process.json")
        if writer.get("status") in {"failed", "timed_out"}:
            return "writer"
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return None


def _verification_ok(result: object) -> bool:
    if isinstance(result, bool):
        return result
    if not isinstance(result, dict):
        return False
    for key in ("verified", "success", "ok"):
        if key in result:
            return result[key] is True
    # A verifier returning a structured report without an explicit negative
    # status is considered valid for compatibility with early implementations.
    return result.get("status") in {"verified", "success", "ok", "passed"}


def _save_acceptance(root: Path, metadata: dict, result: object) -> None:
    if isinstance(result, dict):
        payload = dict(result)
    else:
        payload = {"status": "passed", "verified": True}
    path = root / ".daily-acceptance" / f"{metadata['date']}.json"
    previous = _read_json(path)
    if (payload.get("audio_sha256") and payload["audio_sha256"] == previous.get("audio_sha256")
            and payload.get("edition_id") == previous.get("edition_id")):
        payload["first_accepted_at"] = previous.get("first_accepted_at", previous.get("checked_at"))
        if previous.get("accepted_before_deadline") is True:
            payload["accepted_before_deadline"] = True
    elif payload.get("checked_at"):
        payload["first_accepted_at"] = payload["checked_at"]
    podcast.write_json_atomic(path, payload)


def _publication_state(state: dict, identity: str, max_attempts: int) -> tuple[dict, int]:
    attempts_by_identity = state.setdefault("publication_attempts_by_identity", {})
    if not isinstance(attempts_by_identity, dict):
        attempts_by_identity = {}
        state["publication_attempts_by_identity"] = attempts_by_identity
    try:
        attempts = int(attempts_by_identity.get(identity, 0) or 0)
    except (TypeError, ValueError):
        attempts = 0
    publication = state.get("publication")
    if not isinstance(publication, dict) or publication.get("identity") != identity:
        publication = {"identity": identity, "attempts": attempts, "max_attempts": max_attempts}
        state["publication"] = publication
    else:
        try:
            attempts = max(attempts, int(publication.get("attempts", 0) or 0))
        except (TypeError, ValueError):
            pass
    publication["max_attempts"] = max_attempts
    publication["attempts"] = attempts
    return publication, attempts


def _is_community_material(item: dict) -> bool:
    """Use explicit genre, with a broad host fallback for old payloads."""
    if item.get('source_genre') == 'community_post':
        return True
    if item.get('source_genre') == 'source_report':
        return False
    try:
        host = (urlsplit(str(item.get('url') or '')).hostname or '').lower().removeprefix('www.')
    except ValueError:
        host = ''
    source_id = str(item.get('source_id') or '').casefold()
    return (source_id.startswith('community:')
            or any(host == domain or host.endswith('.' + domain) for domain in COMMUNITY_HOSTS))


def publish_episode(root: Path, metadata: dict, enabled: bool, config: dict | None = None) -> None:
    """Publish one completed episode, with a separate bounded retry budget.

    ``config`` is optional for compatibility with callers from before preview
    publication was introduced.  A configured preview repository adds a
    deploy and remote verification step; it never invokes generation again.
    """
    if not enabled:
        return
    config = config or {}
    marker = root / '.daily-published.json'
    identity = metadata['audio_url']
    repo = Path(__file__).resolve().parents[1]
    publication_config = config.get("publication") if isinstance(config.get("publication"), dict) else {}
    preview_repo_value = publication_config.get("preview_repo")
    preview_repo = Path(str(preview_repo_value)).expanduser().resolve() if preview_repo_value else None
    try:
        max_attempts = max(1, int(publication_config.get("max_attempts", 4) or 4))
    except (TypeError, ValueError):
        max_attempts = 4
    state_path = _state_path(root)
    state = _load_state(root, str(metadata.get("date", "")))
    publication, attempts = _publication_state(state, identity, max_attempts)

    marker_identity = _read_json(marker).get("audio_url") if marker.is_file() else None

    # A legacy local-only publication marker is still authoritative when no
    # preview repository is configured.  Preview mode must verify remotely on
    # every invocation, even when that marker already exists.
    if preview_repo is None and marker_identity == identity:
        return

    # In preview mode a matching marker still requires a remote check.  A
    # stale/missing remote is repaired with deploy-only work, so report build
    # and podcast generation are never repeated for this identity.
    deploy_only = False
    verifier = None
    if preview_repo is not None:
        try:
            from verify_podcast_run import verify_episode
            verifier = verify_episode
        except Exception as exc:
            verifier = None
            verify_import_error = exc
        if marker_identity == identity:
            try:
                if verifier is None:
                    raise verify_import_error
                result = verifier(repo, metadata, preview_repo)
                if _verification_ok(result):
                    _save_acceptance(root, metadata, result)
                    publication.update({"status": "success", "failure_stage": None})
                    state["last_failure"] = None
                    _save_state(state_path, state)
                    return
                deploy_only = True
            except Exception:
                deploy_only = True

    # A healthy matching marker is returned above even at the budget limit;
    # only an actual repair attempt is bounded.
    if attempts >= max_attempts:
        error = RuntimeError("podcast publication retry budget exhausted")
        publication.update({"status": "blocked", "failure_stage": "publication"})
        state["last_failure"] = {
            "stage": "publication", "retryable": False,
            "at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
            "code": "publication_retry_budget_exhausted",
        }
        _save_state(state_path, state)
        raise error

    verification_attempted = False
    try:
        if not deploy_only:
            env = {**os.environ, "REPO_DIR": str(repo)}
            subprocess.run([str(repo / "scripts/publish_reports_site.sh")],
                           env=env, check=True, timeout=600)
        if preview_repo is not None:
            deploy_env = {**os.environ, "REPO_DIR": str(preview_repo),
                          "NEWSRADAR_PRODUCTION_DIR": str(repo)}
            subprocess.run([str(preview_repo / "scripts/deploy_preview.sh")],
                           env=deploy_env, check=True, timeout=600)
            if verifier is None:
                raise verify_import_error
            verification_attempted = True
            result = verifier(repo, metadata, preview_repo)
            if not _verification_ok(result):
                raise RuntimeError("remote podcast verification failed")
            _save_acceptance(root, metadata, result)
    except Exception as error:
        attempts += 1
        failure_stage = "remote_verification" if verification_attempted else "publication"
        publication.update({"attempts": attempts, "status": "failed",
                            "failure_stage": failure_stage})
        state.setdefault("publication_attempts_by_identity", {})[identity] = attempts
        _set_failure(state_path, state, failure_stage, error,
                     retryable=attempts < max_attempts, increment_generation=False,
                     code="remote_verification_failed" if failure_stage == "remote_verification" else "publication_failed")
        raise

    attempts += 1
    state.setdefault("publication_attempts_by_identity", {})[identity] = attempts
    publication.update({"attempts": attempts, "status": "success", "failure_stage": None})
    state["last_failure"] = None
    _save_state(state_path, state)
    # Keep the legacy marker shape stable for old readers and callers.
    podcast.write_json_atomic(marker, {"audio_url": identity, "date": metadata["date"]})


def frozen_daily_input(root: Path, now: datetime) -> Path | None:
    """Freeze the first usable current report, not each retry's changing feed."""
    snapshot = root / ".daily-inputs" / now.date().isoformat() / "daily.html"
    if snapshot.is_file():
        return snapshot
    if not podcast.LATEST_DAILY.is_file():
        return None
    html = podcast.LATEST_DAILY.read_text(encoding="utf-8", errors="replace")
    pack = podcast.embedded_fact_pack(html)
    if pack is not None:
        podcast.validate_fact_pack(pack, now.date())
    else:
        match = re.search(r'生成时间</span>\s*<span class="info-value">(\d{2}-\d{2})', html)
        content = podcast.extract_ai_content(podcast.LATEST_DAILY)
        if not match or match.group(1) != now.strftime("%m-%d") or len(content) < 80 or "AI 热点分析" not in content:
            print('[podcast] waiting for a usable current daily report')
            return None
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=snapshot.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(html)
    temporary.replace(snapshot)
    return snapshot


def frozen_editorial_materials(root: Path, now: datetime, config: dict) -> Path | None:
    from podcast_materials import build_materials, validate_payload
    if config.get('shared_daily_edition', False):
        from trendradar.ai.daily_edition import freeze, podcast_payload
        repo=Path(__file__).resolve().parents[1]
        edition=freeze(repo,settings=config.get('materials'),report_date=now.date())
        snapshot=root/'.daily-inputs'/now.date().isoformat()/'editorial-materials.json'
        payload=podcast_payload(edition)
        if snapshot.exists():
            saved=json.loads(snapshot.read_text())
            if saved.get('shared_edition') != payload['shared_edition']:
                raise ValueError('existing podcast input belongs to another edition; refusing overwrite')
        else:podcast.write_json_atomic(snapshot,payload)
        return snapshot
    snapshot = root / ".daily-inputs" / now.date().isoformat() / "editorial-materials.json"
    if snapshot.is_file():
        try:
            ok, errors = validate_payload(json.loads(snapshot.read_text(encoding="utf-8")), now.date())
        except (ValueError, TypeError):
            ok = False
        if not ok:
            print('[podcast] frozen editorial material is invalid; keeping previous episode for manual review')
            return None
        return snapshot
    repo = Path(__file__).resolve().parents[1]
    settings = config.get("materials") or {}
    payload, report = build_materials(repo, now.date(), root / ".material-cache",
        max_items=int(settings.get("max_items", 6)), max_fetch=int(settings.get("max_fetch", 20)),
        max_age_days=int(settings.get("max_age_days", 3)), budget_seconds=float(settings.get("budget_seconds", 60)))
    podcast.write_json_atomic(snapshot.with_name("selection-review.json"), report)
    if not payload["items"]:
        print('[podcast] no dated substantive source material; keeping previous episode without a writing/TTS call')
        return None
    min_items = max(0, int(settings.get("min_material_items", 0) or 0))
    min_chars = max(0, int(settings.get("min_material_chars", 0) or 0))
    core_items = [item for item in payload["items"] if not _is_community_material(item)]
    core_chars = sum(len(str(item.get("text") or "")) for item in core_items)
    if ((min_items and len(core_items) < min_items)
            or (min_chars and core_chars < min_chars)):
        print(f'[podcast] screened source reports are insufficient ({len(core_items)} items, '
              f'{core_chars} chars; community context excluded); not freezing or writing; will retry collection later')
        return None
    ok, errors = validate_payload(payload, now.date())
    if not ok:
        print('[podcast] material selection did not pass validation; keeping previous episode')
        return None
    podcast.write_json_atomic(snapshot, payload)
    return snapshot


def has_rejected_cached_script(config: dict, day: str) -> bool:
    """Read the durable writer result even if its subprocess error is generic."""
    try:
        runtime=Path(os.environ.get('NEWSRADAR_SOULX_ROOT',str((config.get('soulx') or {}).get('runtime_root','')))).expanduser()
        private=Path(config['dialogue_private_root']).expanduser() if config.get('dialogue_private_root') else runtime/'cache/newsradar-dialogue-editions'
        review=json.loads((private/day/'draft-review.json').read_text())
        return (review.get('date')==day and review.get('mode')=='invalid_model_output'
                and bool(review.get('raw_response')) and Path(review['raw_response']).is_file())
    except (OSError,ValueError,TypeError,KeyError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resume-after-fix', action='store_true', help='explicitly resume saved episode after a verified local pipeline repair; preserve model cache')
    parser.add_argument('--date', type=date.fromisoformat, help='explicit historical episode date; uses archived facts only')
    parser.add_argument('--publish', action='store_true', help='publish a complete episode through the existing site pipeline')
    parser.add_argument('--now', action='store_true', help='ignore the daily time gate; still keep completed episodes')
    args = parser.parse_args()
    config, _ = podcast.load_podcast_config()
    if not config.get('enabled', False):
        return 0
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    if args.date:
        if args.date>now.date():raise ValueError('future episode date rejected')
        now=datetime.combine(args.date,time(22,5),tzinfo=ZoneInfo('Asia/Shanghai'))
    if not args.now and not args.date and (now.hour, now.minute) < (int(config.get('daily_hour', 20)), int(config.get('daily_minute', 30))):
        return 0
    root = podcast.OUTPUT_ROOT
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.daily-worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('[podcast] another daily worker is active')
            return 0
        # Keep a separate receipt for every locked invocation. Completed-date
        # checks are publication-only and must not overwrite generation proof.
        audit_path, audit_receipt = _start_generation_audit(root, now.date().isoformat(), args)
        # A successful day is frozen before reading changing news or using AI.
        index = root / now.date().isoformat() / 'daily.json'
        if index.is_file():
            metadata = _read_json(index)
            state_path = _state_path(root)
            state = _load_state(root, now.date().isoformat())
            if not _episode_bundle(root, index, metadata, config):
                # A date index is an explicit claim that generation completed.
                # Never silently replace a corrupt bundle with a new writer/TTS
                # run; manual repair or removal of the index is required.
                error = RuntimeError('existing completed podcast bundle is invalid; refusing regeneration')
                _finish_generation_audit(audit_path, audit_receipt, status='failed', error=error)
                _set_failure(state_path, state, 'synthesis', error,
                             retryable=False, increment_generation=False,
                             code='invalid_completed_bundle')
                raise error
            audio = root / Path(metadata['audio_url'].removeprefix('/audio/'))
            settings = config.get('podcast', config)
            min_audio = max(0.0, float(settings.get('min_audio_seconds', 0) or 0))
            duration = podcast.measure_duration(audio)
            if metadata.get("edition_variant") == "bulletin":
                from podcast_episode_policy import BULLETIN_MIN_SECONDS
                min_audio = BULLETIN_MIN_SECONDS
            if min_audio and duration < min_audio:
                print(f'[podcast] existing episode is {duration:.1f}s, below new minimum '
                      f'{min_audio:g}s; keeping it without republish or rewrite')
                _finish_generation_audit(audit_path, audit_receipt, status='publication_only', metadata=metadata)
                return 0
            print('[podcast] today already has a complete episode')
            try:
                publish_episode(root, metadata, args.publish, config)
            finally:
                _finish_generation_audit(audit_path, audit_receipt, status='publication_only', metadata=metadata)
            return 0
        state_path = _state_path(root)
        state = _load_state(root, now.date().isoformat())
        if args.resume_after_fix and state.get('failed_attempts', 0):
            audit=root/'.daily-resume-audit'
            audit.mkdir(exist_ok=True)
            podcast.write_json_atomic(audit/(datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d-%H%M%S-%f')+'.json'),
                {'previous_state':state,'reason':'explicit_resume_after_local_pipeline_fix',
                 'model_cache_preserved':True,'resumed_at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()})
            state={**state,'failed_attempts':0}
            state.pop('blocked_reason',None)
            podcast.write_json_atomic(state_path,state)
        if state.get('failed_attempts', 0) >= 2:
            raise RuntimeError('daily retry budget exhausted; inspect logs before manual retry')
        if state.get('blocked_reason') and not args.resume_after_fix:
            raise RuntimeError('episode blocked at '+state['blocked_reason']+'; unchanged inputs will not heal by retry; inspect saved review')
        screened = config.get("source_mode") == "screened_articles"
        source = frozen_editorial_materials(root, now, config) if screened else frozen_daily_input(root, now)
        if source is None:
            _finish_generation_audit(audit_path, audit_receipt, status='skipped')
            return 0
        # Runtime preflight is deliberately limited to the model-dialogue
        # path.  It records diagnostics but does not consume GPU generation
        # retries, since no writer/TTS work has started yet.
        if config.get("script_mode") == "model_dialogue":
            runtime = None
            try:
                import podcast_preflight
                runtime = podcast_preflight.check_runtime(config, Path(__file__).resolve().parents[1])
                state["preflight"] = runtime
                state["runtime"] = runtime  # compatibility/readability alias
                if isinstance(runtime, dict) and any(
                        key in runtime and runtime[key] is False
                        for key in ("ok", "success", "ready")):
                    raise RuntimeError(str(runtime.get("error") or "podcast runtime preflight failed"))
                _save_state(state_path, state)
            except Exception as error:
                # Import and runtime failures are both preflight failures;
                # failed_attempts intentionally remains unchanged.
                if isinstance(runtime, dict):
                    state["preflight"] = {**runtime, "ok": False,
                                           "error_type": type(error).__name__}
                else:
                    state["preflight"] = {"ok": False, "error_type": type(error).__name__}
                state["runtime"] = state["preflight"]
                _set_failure(state_path, state, "preflight", error,
                             retryable=True, increment_generation=False,
                             code="runtime_preflight_failed")
                _finish_generation_audit(audit_path, audit_receipt, status='failed', error=error)
                raise
        generation_args = argparse.Namespace(force=False, input=source, allow_daily_snapshot=True,
            editorial_materials=source if screened else None,
            date=now.date().isoformat() if args.date else None, fact_pack=None, output_root=root, script_only=False,
            reviewed_script_sha256=None, engine=None, once_per_day=True)
        try:
            code = podcast.generate(generation_args)
        except Exception as error:
            _finish_generation_audit(audit_path, audit_receipt, status="failed", error=error)
            # Format/content failures are deterministic for the cached draft.
            # Never spend the GPU retry budget repeating the same rejected raw.
            message=str(error)
            known=('writer review contains warnings','dialogue has warnings',
                   'writer review rejected','dialogue validation failed',
                   'writer response was truncated','this edition already has a writing attempt')
            stage = _failure_stage_from_private_state(config, now.date().isoformat())
            if any(reason in message for reason in known) or has_rejected_cached_script(config, now.date().isoformat()):
                stage = 'script_validation'
            stage = stage or 'writer'
            deterministic = stage == 'script_validation'
            if deterministic:
                state['blocked_reason'] = stage
            _set_failure(state_path, state, stage, error,
                         retryable=not deterministic,
                         increment_generation=True,
                         code='cached_script_rejected' if deterministic else 'generation_failed')
            raise
        if code:
            _finish_generation_audit(audit_path, audit_receipt, status="failed", code=code)
            stage = _failure_stage_from_private_state(config, now.date().isoformat()) or 'writer'
            failure = RuntimeError('podcast generation exited with code ' + str(code))
            deterministic = stage == 'script_validation'
            if deterministic:
                state['blocked_reason'] = stage
            _set_failure(state_path, state, stage, failure,
                         retryable=not deterministic, increment_generation=True,
                         code='cached_script_rejected' if deterministic else 'generation_failed')
        if code == 0 and index.is_file():
            generated_metadata = _read_json(index)
            _finish_generation_audit(audit_path, audit_receipt, status="complete",
                                     metadata=generated_metadata, code=code)
            state.update({'date':now.date().isoformat(), 'failed_attempts':0,
                          'last_completed_at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
                          'last_failure': None})
            _save_state(state_path, state)
            publish_episode(root, generated_metadata, args.publish, config)
        elif code == 0:
            error = RuntimeError("generation completed without a daily bundle")
            _finish_generation_audit(audit_path, audit_receipt, status="failed", error=error, code=code)
        return code


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f'[podcast] daily worker failed: {exc}', file=__import__('sys').stderr)
        raise SystemExit(1)
