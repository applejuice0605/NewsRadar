#!/usr/bin/env python3
"""Accept one published episode against local frozen facts and live assets."""
from __future__ import annotations

import argparse
import base64
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re
import ssl
import subprocess
import time
import urllib.request
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

BASE_URL = 'https://preview.newsradar.ccwu.cc'


class EpisodeVerificationError(RuntimeError):
    """A generated or published episode has not passed acceptance."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise EpisodeVerificationError('unexpected asset redirect')


def _fetch(path: str, token: str, limit: int | None = None) -> tuple[int, dict, bytes]:
    headers = {'Authorization': 'Basic ' + token, 'Cache-Control': 'no-cache',
               'User-Agent': 'NewsRadarAcceptance/1.0'}
    if limit:
        headers['Range'] = f'bytes=0-{limit - 1}'
    import certifi
    opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(
        context=ssl.create_default_context(cafile=certifi.where())))
    request = urllib.request.Request(BASE_URL + path, headers=headers)
    try:
        with opener.open(request, timeout=45) as response:
            return response.status, dict(response.headers), response.read(limit or 2_000_000)
    except Exception as error:
        raise EpisodeVerificationError('online request failed: ' + type(error).__name__) from None


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError('not an object')
        return value
    except (OSError, ValueError) as error:
        raise EpisodeVerificationError('invalid local artifact: ' + path.name) from error


def _audio_file(root: Path, url: str) -> Path:
    parsed = urlsplit(str(url))
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or not parsed.path.startswith('/audio/'):
        raise EpisodeVerificationError('invalid episode asset path')
    path = (root / parsed.path.removeprefix('/audio/')).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file() or not path.stat().st_size:
        raise EpisodeVerificationError('missing episode asset')
    return path


def verify_local(repo: Path, metadata: dict) -> dict:
    root = repo / 'TrendRadar/output/audio'
    day = metadata.get('date')
    try:
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError('date')
    except (TypeError, ValueError):
        raise EpisodeVerificationError('invalid episode date') from None
    if _read(root / day / 'daily.json') != metadata:
        raise EpisodeVerificationError('local date index changed')
    audio = _audio_file(root, metadata.get('audio_url', ''))
    _audio_file(root, metadata.get('transcript_url', ''))
    _audio_file(root, metadata.get('sources_url', ''))
    probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                            '-of', 'default=noprint_wrappers=1:nokey=1', str(audio)],
                           capture_output=True, text=True, check=True, timeout=30)
    duration = float(probe.stdout.strip())
    from podcast_episode_policy import BULLETIN_MIN_SECONDS
    minimum = BULLETIN_MIN_SECONDS if metadata.get("edition_variant") == "bulletin" else 300
    if not minimum <= duration <= 600 or abs(duration - float(metadata.get('duration_seconds', 0))) > 0.2:
        raise EpisodeVerificationError('episode duration failed')
    edition = _read(repo / 'TrendRadar/output/meta/daily_editions' / day / 'edition.json')
    shared = metadata.get('shared_edition') or {}
    for key in ('date', 'edition_id', 'facts_sha256', 'news_cutoff_at'):
        if not edition.get(key) or shared.get(key) != edition[key]:
            raise EpisodeVerificationError('frozen edition mismatch: ' + key)
    return {'date': day, 'edition_variant': metadata.get('edition_variant', 'dialogue'), 'duration_seconds': round(duration, 3),
            'edition_id': shared['edition_id'], 'facts_sha256': shared['facts_sha256'],
            'audio_sha256': hashlib.sha256(audio.read_bytes()).hexdigest()}


def unattended_evidence(root: Path, metadata: dict) -> dict:
    """Require a completed scheduler receipt and retain manual interventions."""
    day = metadata['date']
    receipts = [_read(path) for path in (root / '.daily-run-audit').glob(day + '-*.json')]
    overrides = ('date_override', 'time_override', 'resume_after_fix')
    manual = any(record.get('launchd_service') != 'com.bruce.newsradar.podcast'
                 or any(record.get(key) for key in overrides) for record in receipts)
    for path in (root / '.daily-resume-audit').glob('*.json'):
        if _read(path).get('previous_state', {}).get('date') == day:
            manual = True
    completed = [record for record in receipts if record.get('generation_status') == 'complete'
                 and record.get('audio_url') == metadata['audio_url']]
    scheduled = any(record.get('launchd_service') == 'com.bruce.newsradar.podcast'
                    and all(record.get(key) is False for key in overrides) for record in completed)
    return {'unattended': scheduled and not manual, 'scheduler_receipt_found': scheduled,
            'manual_intervention_recorded': manual}


def _verify_episode_once(repo: Path, metadata: dict, preview_repo: Path) -> dict:
    result = verify_local(repo, metadata)
    # Credentials are used only for this fixed deployment origin.
    values = {}
    for line in (preview_repo / '.preview-auth.local').read_text().splitlines():
        if '=' in line:
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip().strip('\"\'')
    token = base64.b64encode((values.get('PREVIEW_USERNAME', 'newsradar') + ':' +
                             values['PREVIEW_PASSWORD']).encode()).decode()
    status, _, body = _fetch('/audio/' + metadata['date'] + '/daily.json', token)
    try:
        remote = json.loads(body)
    except ValueError:
        raise EpisodeVerificationError('invalid online date index') from None
    if status != 200:
        raise EpisodeVerificationError('online date index unavailable')
    for key in ('date', 'audio_url', 'transcript_url', 'sources_url',
                'script_sha256', 'shared_edition', 'duration_seconds'):
        if not metadata.get(key) or remote.get(key) != metadata[key]:
            raise EpisodeVerificationError('online episode mismatch: ' + key)
    if remote.get("edition_variant", "dialogue") != metadata.get("edition_variant", "dialogue"):
        raise EpisodeVerificationError("online episode variant mismatch")
    latest = _read(repo / 'TrendRadar/output/audio/latest.json')
    status, _, body = _fetch('/audio/latest.json', token)
    try:
        remote_latest = json.loads(body)
    except ValueError:
        raise EpisodeVerificationError('invalid online latest index') from None
    if (status != 200 or remote_latest.get('date') != latest.get('date')
            or remote_latest.get('audio_url') != latest.get('audio_url')
            or latest.get('date', '') < metadata['date']):
        raise EpisodeVerificationError('online latest index is stale or mismatched')
    audio = _audio_file(repo / 'TrendRadar/output/audio', metadata['audio_url'])
    status, headers, body = _fetch(metadata['audio_url'], token, 32)
    headers = {key.lower(): value for key, value in headers.items()}
    expected_range = 'bytes 0-31/' + str(audio.stat().st_size)
    if (status != 206 or not headers.get('content-type', '').startswith('audio/')
            or headers.get('content-range') != expected_range or len(body) != 32
            or body != audio.read_bytes()[:32]):
        raise EpisodeVerificationError('online audio range verification failed')
    size = audio.stat().st_size
    if size > 64_000_000:
        raise EpisodeVerificationError('episode exceeds acceptance download limit')
    full_status, full_headers, full_body = _fetch(metadata['audio_url'], token, size)
    full_headers = {key.lower(): value for key, value in full_headers.items()}
    if (full_status != 206 or len(full_body) != size
            or full_headers.get('content-range') != f'bytes 0-{size - 1}/{size}'
            or hashlib.sha256(full_body).hexdigest() != result['audio_sha256']):
        raise EpisodeVerificationError('online audio full hash verification failed')
    for key in ('transcript_url', 'sources_url'):
        asset_status, _, asset_body = _fetch(metadata[key], token)
        local = _audio_file(repo / 'TrendRadar/output/audio', metadata[key])
        if asset_status != 200 or asset_body != local.read_bytes():
            raise EpisodeVerificationError('online companion asset mismatch: ' + key)
    result.update(status='passed', online_date=remote['date'], latest_date=remote_latest['date'],
                  audio_status=status, bytes_read=len(body), full_audio_hash_verified=True,
                  checked_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat())
    checked = datetime.fromisoformat(result["checked_at"])
    result["accepted_before_deadline"] = checked.date().isoformat() == metadata["date"] and (
        checked.hour, checked.minute) < (23, 15)
    result.update(unattended_evidence(repo / 'TrendRadar/output/audio', metadata))
    return result


def verify_episode(repo: Path, metadata: dict, preview_repo: Path) -> dict:
    """Allow a bounded propagation window without rebuilding or regenerating."""
    for attempt in range(3):
        try:
            return _verify_episode_once(repo, metadata, preview_repo)
        except EpisodeVerificationError:
            if attempt == 2:
                raise
            time.sleep(10)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--preview-repo', type=Path, default=Path('/Users/bruce/Coding/NewsRadar'))
    parser.add_argument('--date', required=True, type=date.fromisoformat)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--require-unattended', action='store_true')
    args = parser.parse_args()
    metadata = _read(args.repo / 'TrendRadar/output/audio' / args.date.isoformat() / 'daily.json')
    try:
        result = verify_episode(args.repo, metadata, args.preview_repo)
        if args.require_unattended and not result['unattended']:
            result.update(status='failed', error='unattended scheduler evidence missing or manual intervention recorded')
    except Exception as error:
        result = {'status': 'failed', 'date': args.date.isoformat(), 'error_type': type(error).__name__,
                  'error': str(error) if isinstance(error, EpisodeVerificationError) else 'acceptance failed'}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
