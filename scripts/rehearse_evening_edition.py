#!/usr/bin/env python3
"""Replay a completed edition through real child interpreters using caches only."""
from __future__ import annotations

import argparse
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from zoneinfo import ZoneInfo

import generate_daily_podcast as podcast
from podcast_dialogue_episode import build_dialogue_bundle
from podcast_preflight import check_runtime
from verify_podcast_run import verify_episode


def cached_runner(command: list[str], **kwargs):
    """Exercise child startup while making provider/GPU cache misses fail closed."""
    script = Path(command[1])
    if script.name == 'generate_dialogue_podcast.py':
        command = [*command, '--cache-only']
    elif script.name == 'soulx_dialogue_synthesize.py':
        # Keep the synthesis implementation byte-identical, since its file hash
        # is part of the audio cache key. Guard the model-load seam in this
        # isolated child rather than changing the backend or cache identities.
        guard = '''import importlib.util,sys
from pathlib import Path
sys.path.insert(0,str(Path(sys.argv[1]).resolve().parent))
spec=importlib.util.spec_from_file_location("rehearsal_soulx",sys.argv[1])
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
def deny_model(*args,**kwargs):
    raise RuntimeError("rehearsal requires a complete synthesis cache")
module._load_model=deny_model
raise SystemExit(module.main(sys.argv[2:]))
'''
        command = [command[0], '-c', guard, *command[1:]]
    else:
        raise RuntimeError('unexpected rehearsal child')
    return subprocess.run(command, **kwargs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', required=True, type=date.fromisoformat)
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    stamp = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S-%f')
    evidence = repo / '.deploy/podcast-rehearsal' / stamp
    evidence.mkdir(parents=True)
    report = {'date': args.date.isoformat(), 'status': 'running', 'phases': {},
              'started_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
              'model_calls': 0, 'new_synthesis_allowed': False}
    result_path = evidence / 'result.json'
    try:
        config, _ = podcast.load_podcast_config()
        report['phases']['preflight'] = check_runtime(config, repo)
        runtime = Path(os.environ.get('NEWSRADAR_SOULX_ROOT',
            str(config['soulx']['runtime_root']))).expanduser().resolve()
        private = Path(config.get('dialogue_private_root') or
                       runtime / 'cache/newsradar-dialogue-editions').expanduser()
        day = args.date.isoformat()
        source = private / day
        materials = json.loads((source / 'input.json').read_text())
        root = podcast.OUTPUT_ROOT
        metadata = json.loads((root / day / 'daily.json').read_text())
        if metadata.get('shared_edition') != materials.get('shared_edition'):
            raise RuntimeError('rehearsal frozen facts disagree with published episode')
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in [root / 'latest.json', root / day / 'daily.json']}
        # Copy only writer artifacts. The backend reads the existing native
        # cache and writes its replay into this private rehearsal directory.
        draft_root = evidence / 'private'
        shutil.copytree(source, draft_root / day,
                        ignore=shutil.ignore_patterns('synthesis', 'writer-process.*', 'synthesis-process.*'))
        summary = build_dialogue_bundle(materials=materials, report_date=args.date,
            prompt_path=repo / 'TrendRadar/config' / config['dialogue_prompt_file'],
            runtime_root=runtime, private_root=draft_root, stage=evidence / 'bundle',
            min_seconds=float(config.get('min_audio_seconds', 300)),
            max_seconds=float(config.get('max_audio_seconds', 600)), command_runner=cached_runner)
        writer = json.loads((draft_root / day / 'draft-review.json').read_text())
        synthesis = json.loads((draft_root / day / 'synthesis/dialogue.metrics.json').read_text())
        if writer.get('additional_model_calls') != 0 or not synthesis.get('cache_hit'):
            raise RuntimeError('rehearsal did not exclusively reuse caches')
        local_audio = root / metadata['audio_url'].removeprefix('/audio/')
        if (summary['script_sha256'] != metadata['script_sha256']
                or hashlib.sha256((evidence / 'bundle/daily.mp3').read_bytes()).digest()
                != hashlib.sha256(local_audio.read_bytes()).digest()):
            raise RuntimeError('replayed bundle changed the completed episode')
        report['phases']['cached_dialogue_and_synthesis'] = {
            'status': 'passed', 'writer_calls': 0, 'synthesis_cache_hit': True,
            'script_sha256': summary['script_sha256'], 'audio_bytes_unchanged': True}
        if args.publish:
            preview = Path(config['publication']['preview_repo']).resolve()
            subprocess.run([str(repo / 'scripts/publish_reports_site.sh')], cwd=repo,
                           env={**os.environ, 'REPO_DIR': str(repo), 'NEWSRADAR_SITE_GIT_PUSH': '0'},
                           check=True, timeout=600)
            subprocess.run([str(preview / 'scripts/deploy_preview.sh')], cwd=preview,
                           env={**os.environ, 'REPO_DIR': str(preview),
                                'NEWSRADAR_PRODUCTION_DIR': str(repo)}, check=True, timeout=600)
            subprocess.run([sys.executable, str(repo / 'scripts/run_daily_podcast.py'),
                            '--date', day, '--publish'], cwd=repo, check=True, timeout=1200)
            report['phases']['publication_and_online_acceptance'] = verify_episode(repo, metadata, preview)
        if any(hashlib.sha256(path.read_bytes()).hexdigest() != digest
               for path, digest in before.items()):
            raise RuntimeError('rehearsal changed completed date/latest indices')
        report['status'] = 'passed'
    except Exception as error:
        report.update(status='failed', error_type=type(error).__name__, error=str(error))
    report['completed_at'] = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
    result_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'status': report['status'], 'evidence': str(result_path)}, ensure_ascii=False))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
