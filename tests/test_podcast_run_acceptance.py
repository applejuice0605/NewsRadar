"""Catch stale deployment and false-positive episode acceptance."""
import json
import fcntl
import os
from pathlib import Path
import sys
import tempfile
import subprocess
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import verify_podcast_run as verify
import rehearse_evening_edition as rehearsal


class EpisodeAcceptanceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        day = '2026-11-15'
        shared = dict(date=day, edition_id='edition', facts_sha256='facts', news_cutoff_at=day+'T22:00:00+08:00')
        self.metadata = dict(date=day, audio_url=f'/audio/{day}/bundle/daily.mp3',
            transcript_url=f'/audio/{day}/bundle/daily.txt', sources_url=f'/audio/{day}/bundle/sources.json',
            duration_seconds=403.76, script_sha256='script', shared_edition=shared)
        root = self.repo / 'TrendRadar/output'
        (root / f'audio/{day}/bundle').mkdir(parents=True)
        (root / f'audio/{day}/bundle/daily.mp3').write_bytes(b'a'*64)
        for name in ('daily.txt', 'sources.json'):
            (root / f'audio/{day}/bundle' / name).write_text('fixture')
        (root / f'audio/{day}/daily.json').write_text(json.dumps(self.metadata))
        (root / 'audio/latest.json').write_text(json.dumps(self.metadata))
        (root / f'meta/daily_editions/{day}').mkdir(parents=True)
        (root / f'meta/daily_editions/{day}/edition.json').write_text(json.dumps(shared))
        (self.repo / '.preview-auth.local').write_text('PREVIEW_USERNAME=fixture\nPREVIEW_PASSWORD=fixture\n')
        self.probe = patch.object(verify.subprocess, 'run', return_value=types.SimpleNamespace(stdout='403.76'))
        self.probe.start()
        self.addCleanup(self.probe.stop)
        sleep_patch = patch.object(verify.time, 'sleep')
        self.sleep = sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def fetch(self, path, token, limit=None):
        if limit:
            return 206, {'Content-Type': 'audio/mpeg', 'Content-Range': f'bytes 0-{limit-1}/64'}, b'a'*limit
        if path.endswith(('.txt', 'sources.json')):
            return 200, {}, b'fixture'
        return 200, {}, json.dumps(self.metadata).encode()

    def test_same_facts_and_live_audio_pass(self):
        with patch.object(verify, '_fetch', side_effect=self.fetch):
            result = verify.verify_episode(self.repo, self.metadata, self.repo)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['bytes_read'], 32)

    def test_short_audio_is_accepted_only_for_explicit_bulletin(self):
        root = self.repo / 'TrendRadar/output/audio'
        index = root / self.metadata['date'] / 'daily.json'
        latest = root / 'latest.json'
        self.metadata['duration_seconds'] = 120
        with patch.object(verify.subprocess, 'run', return_value=types.SimpleNamespace(stdout='120')):
            index.write_text(json.dumps(self.metadata))
            with self.assertRaisesRegex(verify.EpisodeVerificationError, 'duration'):
                verify.verify_local(self.repo, self.metadata)
            self.metadata['edition_variant'] = 'bulletin'
            for path in (index, latest):
                path.write_text(json.dumps(self.metadata))
            with patch.object(verify, '_fetch', side_effect=self.fetch):
                result = verify.verify_episode(self.repo, self.metadata, self.repo)
            self.assertEqual(result['edition_variant'], 'bulletin')
            self.assertTrue(result['full_audio_hash_verified'])

    def test_propagation_retry_is_bounded_and_does_not_redeploy(self):
        with patch.object(verify, '_verify_episode_once', side_effect=[
                verify.EpisodeVerificationError('old edge'), {'status':'passed'}]) as once:
            self.assertEqual(verify.verify_episode(self.repo, self.metadata, self.repo)['status'], 'passed')
        self.assertEqual(once.call_count, 2)
        self.sleep.assert_called_once_with(10)
        self.sleep.reset_mock()
        with patch.object(verify, '_verify_episode_once', side_effect=verify.EpisodeVerificationError('bad')) as once:
            with self.assertRaises(verify.EpisodeVerificationError):
                verify.verify_episode(self.repo, self.metadata, self.repo)
        self.assertEqual(once.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)

    def test_stale_latest_fails_even_when_target_audio_works(self):
        def fetch(path, token, limit=None):
            if path == '/audio/latest.json':
                return 200, {}, json.dumps({**self.metadata, 'date':'2026-11-14'}).encode()
            return self.fetch(path, token, limit)
        with patch.object(verify, '_fetch', side_effect=fetch), self.assertRaisesRegex(
                verify.EpisodeVerificationError, 'latest'):
            verify.verify_episode(self.repo, self.metadata, self.repo)

    def test_facts_mismatch_fails_before_network(self):
        self.metadata['shared_edition']['facts_sha256'] = 'changed'
        root = self.repo/'TrendRadar/output/audio/2026-11-15/daily.json'
        root.write_text(json.dumps(self.metadata))
        with patch.object(verify, '_fetch') as fetch, self.assertRaisesRegex(
                verify.EpisodeVerificationError, 'frozen edition'):
            verify.verify_episode(self.repo, self.metadata, self.repo)
        fetch.assert_not_called()

    def test_wrong_audio_length_or_bytes_cannot_pass_range(self):
        def fetch(path, token, limit=None):
            if limit:
                return 206, {'Content-Type':'audio/mpeg', 'Content-Range':'bytes 0-31/100'}, b'a'*32
            return self.fetch(path, token, limit)
        with patch.object(verify, '_fetch', side_effect=fetch), self.assertRaisesRegex(
                verify.EpisodeVerificationError, 'range'):
            verify.verify_episode(self.repo, self.metadata, self.repo)

    def test_audio_path_cannot_escape_root(self):
        with self.assertRaises(verify.EpisodeVerificationError):
            verify._audio_file(self.repo, '/audio/../../private')

    def test_changed_audio_after_header_is_rejected(self):
        def fetch(path, token, limit=None):
            status, headers, body = self.fetch(path, token, limit)
            if limit == 64:
                body = body[:40] + b'x' + body[41:]
            return status, headers, body
        with patch.object(verify, '_fetch', side_effect=fetch), self.assertRaisesRegex(
                verify.EpisodeVerificationError, 'full hash'):
            verify.verify_episode(self.repo, self.metadata, self.repo)

    def test_mismatched_companion_url_is_rejected(self):
        def fetch(path, token, limit=None):
            if path.endswith('/daily.json'):
                return 200, {}, json.dumps({**self.metadata, 'transcript_url':'/audio/old.txt'}).encode()
            return self.fetch(path, token, limit)
        with patch.object(verify, '_fetch', side_effect=fetch), self.assertRaisesRegex(
                verify.EpisodeVerificationError, 'transcript_url'):
            verify.verify_episode(self.repo, self.metadata, self.repo)

    def test_unattended_requires_scheduler_receipt_and_no_manual_override(self):
        root = self.repo/'TrendRadar/output/audio'
        self.assertFalse(verify.unattended_evidence(root, self.metadata)['unattended'])
        audit = root/'.daily-run-audit'
        audit.mkdir()
        receipt = dict(date=self.metadata['date'], launchd_service='com.bruce.newsradar.podcast',
                       generation_status='complete', audio_url=self.metadata['audio_url'],
                       date_override=False, time_override=False, resume_after_fix=False)
        (audit/'2026-11-15-one.json').write_text(json.dumps(receipt))
        self.assertTrue(verify.unattended_evidence(root, self.metadata)['unattended'])
        (audit/'2026-11-15-two.json').write_text(json.dumps({**receipt, 'resume_after_fix':True}))
        self.assertFalse(verify.unattended_evidence(root, self.metadata)['unattended'])


class RehearsalGuardsTest(unittest.TestCase):
    def test_preview_deploy_lock_defers_a_competing_publisher(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.deploy').mkdir()
            with (root / '.deploy/.preview-deploy.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                env = {**os.environ, 'REPO_DIR': str(root)}
                env.pop('NEWSRADAR_PREVIEW_DEPLOY_LOCK_HELD', None)
                command = Path(__file__).resolve().parents[1] / 'scripts/deploy_preview.sh'
                result = subprocess.run([str(command)], env=env, capture_output=True,
                                        text=True, timeout=10)
            self.assertEqual(result.returncode, 75, result.stderr)
            self.assertIn('another deployment', result.stdout)

    def test_writer_requires_cache_only(self):
        with patch.object(rehearsal.subprocess, 'run') as run:
            rehearsal.cached_runner(['python', '/repo/scripts/generate_dialogue_podcast.py'], check=True)
        self.assertEqual(run.call_args.args[0][-1], '--cache-only')

    def test_speech_child_disables_model_loading(self):
        with patch.object(rehearsal.subprocess, 'run') as run:
            rehearsal.cached_runner(['/runtime/python', '/repo/scripts/soulx_dialogue_synthesize.py', '--script', 'saved.json'])
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ['/runtime/python', '-c'])
        self.assertIn('module._load_model=deny_model', command[2])
        self.assertEqual(command[3:], ['/repo/scripts/soulx_dialogue_synthesize.py', '--script', 'saved.json'])


if __name__ == '__main__':
    unittest.main()
