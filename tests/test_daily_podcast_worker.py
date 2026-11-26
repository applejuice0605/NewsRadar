"""The daily worker must freeze success and keep GPU work outside news jobs."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
from datetime import datetime
from zoneinfo import ZoneInfo

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/run_daily_podcast.py'

class DailyWorkerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.podcast = types.SimpleNamespace(OUTPUT_ROOT=self.root, LATEST_DAILY=self.root/'daily.html',
            load_podcast_config=Mock(return_value=({'enabled': True}, {})),
            embedded_fact_pack=Mock(return_value=None), validate_fact_pack=Mock(),
            extract_ai_content=Mock(return_value='AI 热点分析' + '已完成的分析材料。' * 12),
            generate=Mock(return_value=0), write_json_atomic=Mock(), validate_duration=Mock(), measure_duration=Mock(return_value=30))
        today = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%m-%d')
        self.podcast.LATEST_DAILY.write_text(f'生成时间</span><span class="info-value">{today}</span>原始新闻材料')
        spec = importlib.util.spec_from_file_location('daily_worker_test', SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'generate_daily_podcast': self.podcast}):
            spec.loader.exec_module(self.module)

    def test_complete_day_skips_writer_before_reading_news(self):
        today = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        day = self.root/today
        day.mkdir()
        for suffix in ('.mp3', '.txt', '.json'):
            (day/('episode'+suffix)).write_text('fixture')
        (day/'daily.json').write_text(json.dumps({'date': today, 'audio_url':f'/audio/{today}/episode.mp3'}))
        with patch.object(sys, 'argv', ['worker', '--now']):
            self.assertEqual(self.module.main(), 0)
        self.podcast.generate.assert_not_called()

    def test_wrong_date_index_fails_closed_without_regeneration(self):
        today = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        day = self.root / today
        day.mkdir()
        for suffix in ('.mp3', '.txt', '.json'):
            (day / ('episode' + suffix)).write_text('fixture')
        (day / 'daily.json').write_text(json.dumps({
            'date': '2026-01-01', 'audio_url': f'/audio/{today}/episode.mp3',
        }))
        self.podcast.write_json_atomic.side_effect = self._real_atomic
        with patch.object(sys, 'argv', ['worker', '--now']), self.assertRaisesRegex(RuntimeError, 'invalid'):
            self.module.main()
        self.podcast.generate.assert_not_called()

    def test_missing_day_invokes_once_per_day_generator(self):
        with patch.object(sys, 'argv', ['worker', '--now']):
            self.assertEqual(self.module.main(), 0)
        args = self.podcast.generate.call_args.args[0]
        self.assertTrue(args.once_per_day)
        self.assertFalse(args.force)

    def test_historical_date_is_passed_to_generator_without_reading_live_report(self):
        self.podcast.load_podcast_config.return_value=({'enabled':True,'source_mode':'screened_articles'}, {})
        snapshot=self.root/'archived-materials.json'
        snapshot.write_text('{}')
        with patch.object(sys,'argv',['worker','--date','2026-11-05']), patch.object(self.module,'frozen_editorial_materials',return_value=snapshot) as freeze:
            self.assertEqual(self.module.main(),0)
        self.assertEqual(freeze.call_args.args[1].date().isoformat(),'2026-11-05')
        self.assertEqual(self.podcast.generate.call_args.args[0].date,'2026-11-05')
        self.assertEqual(self.podcast.generate.call_args.args[0].input,snapshot)

    def test_another_worker_skips_without_writing(self):
        with patch.object(sys, 'argv', ['worker', '--now']), patch.object(self.module.fcntl, 'flock', side_effect=BlockingIOError):
            self.assertEqual(self.module.main(), 0)
        self.podcast.generate.assert_not_called()

    def test_deterministic_script_failure_blocks_repeat_and_explicit_resume_keeps_cache(self):
        self.podcast.write_json_atomic.side_effect=lambda p,d:p.write_text(json.dumps(d))
        self.podcast.generate.side_effect=RuntimeError('writer review contains warnings; human review is required')
        with patch.object(sys,'argv',['worker','--now']),self.assertRaises(RuntimeError):self.module.main()
        state=json.loads((self.root/'.daily-worker-state.json').read_text())
        self.assertEqual(state['blocked_reason'],'script_validation')
        self.assertFalse(state['last_failure']['retryable'])
        with patch.object(sys,'argv',['worker','--now']),self.assertRaisesRegex(RuntimeError,'unchanged inputs'):
            self.module.main()
        self.assertEqual(self.podcast.generate.call_count,1)
        self.podcast.generate.side_effect=None;self.podcast.generate.return_value=0
        with patch.object(sys,'argv',['worker','--now','--resume-after-fix']):self.assertEqual(self.module.main(),0)
        self.assertEqual(self.podcast.generate.call_count,2)
        audit=list((self.root/'.daily-resume-audit').glob('*.json'))
        self.assertEqual(len(audit),1);self.assertTrue(json.loads(audit[0].read_text())['model_cache_preserved'])

    def test_generic_subprocess_error_uses_saved_invalid_writer_review(self):
        import subprocess
        today=datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        runtime=self.root/'runtime';private=runtime/'cache/newsradar-dialogue-editions'/today
        private.mkdir(parents=True);raw=private/'raw.txt';raw.write_text('invalid saved response')
        (private/'draft-review.json').write_text(json.dumps({'date':today,'mode':'invalid_model_output','raw_response':str(raw)}))
        self.podcast.load_podcast_config.return_value=({'enabled':True,'soulx':{'runtime_root':str(runtime)}},{})
        self.podcast.write_json_atomic.side_effect=lambda p,d:p.write_text(json.dumps(d))
        self.podcast.generate.side_effect=subprocess.CalledProcessError(1,['python','generate_dialogue_podcast.py'])
        with patch.object(sys,'argv',['worker','--now']),self.assertRaises(subprocess.CalledProcessError):self.module.main()
        self.assertEqual(json.loads((self.root/'.daily-worker-state.json').read_text())['blocked_reason'],'script_validation')

    def test_two_failed_attempts_stop_automatic_gpu_retries(self):
        today = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        (self.root/'.daily-worker-state.json').write_text(json.dumps({'date': today, 'failed_attempts': 2}))
        with patch.object(sys, 'argv', ['worker', '--now']), self.assertRaisesRegex(RuntimeError, 'retry budget'):
            self.module.main()
        self.podcast.generate.assert_not_called()

    def test_publisher_failure_on_complete_episode_never_resynthesizes(self):
        today = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        day = self.root/today
        day.mkdir()
        for suffix in ('.mp3', '.txt', '.json'):
            (day/('episode'+suffix)).write_text('fixture')
        (day/'daily.json').write_text(json.dumps({'date': today, 'audio_url':f'/audio/{today}/episode.mp3'}))
        with patch.object(sys, 'argv', ['worker', '--now', '--publish']), patch.object(self.module, 'publish_episode', side_effect=RuntimeError('publish failed')), self.assertRaisesRegex(RuntimeError, 'publish failed'):
            self.module.main()
        self.podcast.generate.assert_not_called()

    def test_existing_short_episode_is_kept_without_republish_under_new_minimum(self):
        today = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        day = self.root/today
        day.mkdir()
        for suffix in ('.mp3', '.txt', '.json'):
            (day/('episode'+suffix)).write_text('fixture')
        (day/'daily.json').write_text(json.dumps({'date': today, 'audio_url':f'/audio/{today}/episode.mp3'}))
        self.podcast.load_podcast_config.return_value = ({'enabled': True, 'min_audio_seconds': 300}, {})
        self.podcast.measure_duration.return_value = 30
        with patch.object(sys, 'argv', ['worker', '--now']), patch.object(self.module, 'publish_episode') as publish:
            self.assertEqual(self.module.main(), 0)
        self.podcast.generate.assert_not_called()
        publish.assert_not_called()

    def test_successful_publication_records_identity_without_local_paths(self):
        with patch.object(self.module.subprocess, 'run') as run:
            self.module.publish_episode(self.root, {'date':'2026-10-30','audio_url':'/audio/2026-10-30/abc/daily.mp3'}, True)
        run.assert_called_once()
        marker = self.podcast.write_json_atomic.call_args.args[1]
        self.assertEqual(set(marker), {'date', 'audio_url'})

    def test_failed_generation_reuses_frozen_material_after_news_refresh(self):
        self.podcast.generate.side_effect = RuntimeError('synthetic failure')
        original = self.podcast.LATEST_DAILY.read_text()
        with patch.object(sys, 'argv', ['worker', '--now']), self.assertRaises(RuntimeError):
            self.module.main()
        frozen = self.podcast.generate.call_args.args[0].input
        self.assertEqual(frozen.read_text(), original)
        self.podcast.LATEST_DAILY.write_text(original + '新一轮新闻不应进入本次重试')
        self.podcast.generate.side_effect = None
        with patch.object(sys, 'argv', ['worker', '--now']):
            self.assertEqual(self.module.main(), 0)
        self.assertEqual(self.podcast.generate.call_args.args[0].input, frozen)
        self.assertEqual(frozen.read_text(), original)

    def test_empty_cached_editorial_materials_never_reach_generation(self):
        today=datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        path=self.root/'.daily-inputs'/today/'editorial-materials.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'schema_version':1,'policy_version':'podcast-materials-v1','date':today,'items':[]}))
        module=types.SimpleNamespace(build_materials=Mock(),validate_payload=Mock(return_value=(False,['items_empty'])))
        self.podcast.load_podcast_config.return_value=({'enabled':True,'source_mode':'screened_articles'}, {})
        with patch.dict(sys.modules,{'podcast_materials':module}), patch.object(sys,'argv',['worker','--now']):
            self.assertEqual(self.module.main(),0)
        module.build_materials.assert_not_called()
        self.podcast.generate.assert_not_called()

    def test_new_editorial_materials_below_sufficiency_threshold_are_not_frozen(self):
        today = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        payload = {'schema_version': 1, 'policy_version': 'podcast-materials-v1', 'date': today,
                   'items': [{'text': '短正文'}] * 2}
        module = types.SimpleNamespace(build_materials=Mock(return_value=(payload, {'accepted_count': 2})),
                                       validate_payload=Mock(return_value=(True, [])))
        self.podcast.load_podcast_config.return_value = ({'enabled': True, 'source_mode': 'screened_articles',
            'materials': {'min_material_items': 3, 'min_material_chars': 3000}}, {})
        with patch.dict(sys.modules, {'podcast_materials': module}), patch.object(sys, 'argv', ['worker', '--now']):
            self.assertEqual(self.module.main(), 0)
        module.build_materials.assert_called_once()
        self.podcast.generate.assert_not_called()

    def test_community_text_cannot_satisfy_core_material_threshold(self):
        today = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        payload = {'schema_version': 1, 'policy_version': 'podcast-materials-v1', 'date': today,
                   'items': [
                       {'text': '报道正文一' * 400, 'source_genre': 'source_report'},
                       {'text': '报道正文二' * 400, 'source_genre': 'source_report'},
                       {'text': '社区传闻' * 500, 'source_genre': 'community_post'},
                   ]}
        module = types.SimpleNamespace(build_materials=Mock(return_value=(payload, {'accepted_count': 3})),
                                       validate_payload=Mock(return_value=(True, [])))
        self.podcast.load_podcast_config.return_value = ({'enabled': True, 'source_mode': 'screened_articles',
            'materials': {'min_material_items': 3, 'min_material_chars': 3000}}, {})
        with patch.dict(sys.modules, {'podcast_materials': module}), patch.object(sys, 'argv', ['worker', '--now']):
            self.assertEqual(self.module.main(), 0)
        self.podcast.generate.assert_not_called()

    def _real_atomic(self, path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding='utf-8')

    def _preview_config(self, preview):
        (preview / 'scripts').mkdir(parents=True, exist_ok=True)
        (preview / 'scripts' / 'deploy_preview.sh').write_text('#!/bin/sh\n')
        return {'publication': {'preview_repo': str(preview), 'max_attempts': 4}}

    def test_build_failure_records_publication_stage(self):
        self.podcast.write_json_atomic.side_effect = self._real_atomic
        config = self._preview_config(self.root / 'preview')
        metadata = {'date': '2026-11-15', 'audio_url': '/audio/2026-11-15/daily.mp3'}
        with patch.object(self.module.subprocess, 'run', side_effect=RuntimeError('build failed')):
            with self.assertRaisesRegex(RuntimeError, 'build failed'):
                self.module.publish_episode(self.root, metadata, True, config)
        state = json.loads((self.root / '.daily-worker-state.json').read_text())
        self.assertEqual(state['last_failure']['stage'], 'publication')
        self.assertEqual(state['publication']['failure_stage'], 'publication')
        self.assertEqual(state['failed_attempts'], 0)

    def test_deploy_failure_records_publication_stage_without_masking_error(self):
        self.podcast.write_json_atomic.side_effect = self._real_atomic
        config = self._preview_config(self.root / 'preview')
        metadata = {'date': '2026-11-15', 'audio_url': '/audio/2026-11-15/daily.mp3'}
        with patch.object(self.module.subprocess, 'run', side_effect=[None, RuntimeError('deploy failed')]):
            with self.assertRaisesRegex(RuntimeError, 'deploy failed'):
                self.module.publish_episode(self.root, metadata, True, config)
        state = json.loads((self.root / '.daily-worker-state.json').read_text())
        self.assertEqual(state['last_failure']['stage'], 'publication')
        self.assertEqual(state['publication']['failure_stage'], 'publication')

    def test_remote_mismatch_does_not_write_success_marker(self):
        self.podcast.write_json_atomic.side_effect = self._real_atomic
        config = self._preview_config(self.root / 'preview')
        metadata = {'date': '2026-11-15', 'audio_url': '/audio/2026-11-15/daily.mp3'}
        verifier = types.ModuleType('verify_podcast_run')
        verifier.verify_episode = Mock(return_value={'status': 'failed'})
        with patch.dict(sys.modules, {'verify_podcast_run': verifier}), patch.object(self.module.subprocess, 'run'):
            with self.assertRaisesRegex(RuntimeError, 'remote podcast verification failed'):
                self.module.publish_episode(self.root, metadata, True, config)
        self.assertFalse((self.root / '.daily-published.json').exists())
        state = json.loads((self.root / '.daily-worker-state.json').read_text())
        self.assertEqual(state['last_failure']['stage'], 'remote_verification')

    def test_publication_budget_is_independent_from_generation_budget(self):
        self.podcast.write_json_atomic.side_effect = self._real_atomic
        metadata = {'date': '2026-11-15', 'audio_url': '/audio/2026-11-15/daily.mp3'}
        (self.root / '.daily-worker-state.json').write_text(json.dumps({
            'date': metadata['date'], 'failed_attempts': 2,
        }))
        with patch.object(self.module.subprocess, 'run', side_effect=RuntimeError('publish failed')):
            with self.assertRaisesRegex(RuntimeError, 'publish failed'):
                self.module.publish_episode(self.root, metadata, True, {'publication': {'max_attempts': 4}})
        state = json.loads((self.root / '.daily-worker-state.json').read_text())
        self.assertEqual(state['failed_attempts'], 2)
        self.assertEqual(state['publication']['attempts'], 1)

    def test_healthy_marker_at_publication_budget_still_reverifies(self):
        self.podcast.write_json_atomic.side_effect = self._real_atomic
        metadata = {'date': '2026-11-15', 'audio_url': '/audio/2026-11-15/daily.mp3'}
        (self.root / '.daily-published.json').write_text(json.dumps(metadata))
        (self.root / '.daily-worker-state.json').write_text(json.dumps({
            'date': metadata['date'], 'failed_attempts': 2,
            'publication_attempts_by_identity': {metadata['audio_url']: 4},
        }))
        config = self._preview_config(self.root / 'preview')
        verifier = types.ModuleType('verify_podcast_run')
        verifier.verify_episode = Mock(return_value={'status': 'passed'})
        with patch.dict(sys.modules, {'verify_podcast_run': verifier}), patch.object(self.module.subprocess, 'run') as run:
            self.module.publish_episode(self.root, metadata, True, config)
        verifier.verify_episode.assert_called_once()
        run.assert_not_called()
        self.assertEqual(json.loads((self.root / '.daily-acceptance' / (metadata['date'] + '.json')).read_text())['status'], 'passed')

    def test_preflight_failure_does_not_consume_generation_retry(self):
        today = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        source = self.root / 'materials.json'
        source.write_text('{}')
        self.podcast.load_podcast_config.return_value = ({
            'enabled': True, 'source_mode': 'screened_articles', 'script_mode': 'model_dialogue',
        }, {})
        self.podcast.write_json_atomic.side_effect = self._real_atomic
        preflight = types.ModuleType('podcast_preflight')
        preflight.check_runtime = Mock(side_effect=RuntimeError('MPS unavailable'))
        with patch.dict(sys.modules, {'podcast_preflight': preflight}), patch.object(self.module, 'frozen_editorial_materials', return_value=source), patch.object(sys, 'argv', ['worker', '--now']):
            with self.assertRaisesRegex(RuntimeError, 'MPS unavailable'):
                self.module.main()
        state = json.loads((self.root / '.daily-worker-state.json').read_text())
        self.assertEqual(state['failed_attempts'], 0)
        self.assertEqual(state['last_failure']['stage'], 'preflight')
        self.podcast.generate.assert_not_called()

    def test_generation_receipt_is_completed_for_manual_fixture(self):
        today = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        source = self.root / 'daily.html'
        source.write_text('fixture')
        self.podcast.write_json_atomic.side_effect = self._real_atomic
        def generate(args):
            day = self.root / today
            day.mkdir(parents=True)
            for suffix in ('.mp3', '.txt', '.json'):
                (day / ('daily' + suffix)).write_text('fixture')
            (day / 'daily.json').write_text(json.dumps({'date': today, 'audio_url': f'/audio/{today}/daily.mp3'}))
            return 0
        self.podcast.generate.side_effect = generate
        with patch.object(self.module, 'frozen_daily_input', return_value=source), patch.object(sys, 'argv', ['worker', '--now']):
            self.assertEqual(self.module.main(), 0)
        receipts = list((self.root / '.daily-run-audit').glob('*.json'))
        self.assertEqual(len(receipts), 1)
        receipt = json.loads(receipts[0].read_text())
        self.assertEqual(receipt['generation_status'], 'complete')
        self.assertEqual(receipt['audio_url'], f'/audio/{today}/daily.mp3')
        self.assertFalse(receipt['date_override'])
        self.assertTrue(receipt['time_override'])

    def test_explicit_launchagent_label_overrides_unusable_xpc_name(self):
        import argparse
        self.podcast.write_json_atomic.side_effect = self._real_atomic
        args = argparse.Namespace(date=None, now=False, resume_after_fix=False)
        with patch.dict(self.module.os.environ, {
                'XPC_SERVICE_NAME': '0',
                'NEWSRADAR_LAUNCHD_LABEL': 'com.bruce.newsradar.podcast'}):
            path, _ = self.module._start_generation_audit(self.root, '2026-11-16', args)
        receipt = json.loads(path.read_text())
        self.assertEqual(receipt['launchd_service'], 'com.bruce.newsradar.podcast')
        self.assertFalse(receipt['date_override'])
        self.assertFalse(receipt['time_override'])
