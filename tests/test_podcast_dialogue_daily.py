"""Daily dialogue publication gates; no provider or GPU calls."""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'TrendRadar'), str(ROOT / 'scripts')]
spec = importlib.util.spec_from_file_location('dialogue_daily_tested', ROOT / 'scripts/generate_daily_podcast.py')
module = importlib.util.module_from_spec(spec)
with patch.dict(os.environ, {'LITELLM_LOCAL_MODEL_COST_MAP': 'True'}):
    spec.loader.exec_module(module)

class DailyDialogueTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / 'runtime'
        python = self.runtime / '.venv/bin/python'
        python.parent.mkdir(parents=True)
        python.touch()
        self.prompt = self.root / 'dialogue.txt'
        self.prompt.write_text('prompt')
        self.out = self.root / 'audio'
        self.now = datetime(2026, 10, 31, 12, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.day = self.now.date()
        self.materials = {'date': self.day.isoformat(), 'items': [], 'policy_version': 'test'}
        self.settings = {'engine': 'soulx', 'soulx': {'runtime_root': str(self.runtime)},
                         'dialogue_prompt_file': str(self.prompt)}
        self.args = argparse.Namespace(engine=None, force=False, script_only=False, editorial_draft_only=False)
        self.env = patch.dict(os.environ, {'NEWSRADAR_SOULX_ROOT': str(self.runtime)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def call(self):
        return module.generate_dialogue_daily(self.args, self.settings, self.materials, self.day, self.now, self.out)

    def fake_bundle(self, **kw):
        kw['stage'].mkdir(parents=True)
        (kw['stage'] / 'daily.mp3').write_bytes(b'complete-audio')
        (kw['stage'] / 'daily.txt').write_text('知远：完整的口播正文')
        return {'duration_seconds': 420, 'material_sha256': 'material', 'script_sha256': 'script',
                'engine': 'soulx', 'voice': 'male_1+female_1', 'format': 'daily_dialogue'}

    def test_complete_publish_then_skip_without_new_calls(self):
        with patch('podcast_dialogue_episode.build_dialogue_bundle', side_effect=self.fake_bundle) as build, patch.object(module, 'measure_duration', return_value=420):
            self.assertEqual(self.call(), 0)
            previous = (self.out / 'latest.json').read_bytes()
            self.assertEqual(self.call(), 0)
            self.assertEqual(build.call_count, 1)
            self.assertEqual(previous, (self.out / 'latest.json').read_bytes())
        metadata = json.loads(previous)
        self.assertFalse(metadata['claim_grounded'])
        self.assertFalse(metadata['source_claims_validated'])
        self.assertEqual(metadata['script_mode'], 'model_dialogue')

    def test_failure_leaves_previous_index_unchanged(self):
        self.out.mkdir()
        old = b'{"date":"2026-10-30"}'
        (self.out / 'latest.json').write_bytes(old)
        with patch('podcast_dialogue_episode.build_dialogue_bundle', side_effect=ValueError('failed gate')):
            with self.assertRaisesRegex(ValueError, 'failed gate'):
                self.call()
        self.assertEqual((self.out / 'latest.json').read_bytes(), old)
        self.assertFalse((self.out / self.day.isoformat() / 'daily.json').exists())

    def test_preflight_rejects_before_writer(self):
        for change in ('engine', 'runtime', 'prompt', 'bounds', 'force', 'draft', 'materials'):
            with self.subTest(change=change), patch('podcast_dialogue_episode.build_dialogue_bundle') as build:
                original = dict(self.settings)
                original_materials = self.materials
                if change == 'engine': self.settings['engine'] = 'edge'
                if change == 'runtime': self.runtime.joinpath('.venv/bin/python').unlink()
                if change == 'prompt': self.settings['dialogue_prompt_file'] = '/missing/prompt'
                if change == 'bounds': self.settings['min_audio_seconds'] = 601
                if change == 'force': self.args.force = True
                if change == 'draft': self.args.editorial_draft_only = True
                if change == 'materials': self.materials = None
                with self.assertRaises(ValueError): self.call()
                build.assert_not_called()
                self.settings = original
                self.materials = original_materials
                self.args.force = self.args.editorial_draft_only = False
                self.runtime.joinpath('.venv/bin/python').touch()

    def test_external_audio_url_cannot_trigger_skip(self):
        day = self.out / self.day.isoformat()
        day.mkdir(parents=True)
        (day / 'daily.json').write_text(json.dumps({'date': self.day.isoformat(), 'audio_url': '/audio/../../escape', 'transcript_url': '/audio/../../escape'}))
        with patch('podcast_dialogue_episode.build_dialogue_bundle', side_effect=ValueError('new candidate')) as build:
            with self.assertRaisesRegex(ValueError, 'new candidate'): self.call()
            self.assertEqual(build.call_count, 1)

if __name__ == '__main__': unittest.main()
