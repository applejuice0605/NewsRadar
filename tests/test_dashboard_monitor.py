import importlib.util
import json
import base64
import hmac
import hashlib
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch
from pathlib import Path

SPEC = importlib.util.spec_from_file_location('dashboard', Path(__file__).parents[1] / 'scripts/monitor_dashboard.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class DashboardMonitorTests(unittest.TestCase):
    def test_daily_audio_deadline_is_2315_shanghai(self):
        self.assertFalse(MODULE.daily_health_deadline_passed(datetime(2026, 10, 30, 23, 14)))
        self.assertTrue(MODULE.daily_health_deadline_passed(datetime(2026, 10, 30, 23, 15)))
        self.assertEqual(MODULE.expected_podcast_date(datetime(2026, 10, 30, 23, 14)), '2026-10-29')
        self.assertEqual(MODULE.expected_podcast_date(datetime(2026, 10, 30, 23, 15)), '2026-10-30')
        self.assertEqual(MODULE.expected_podcast_date(datetime(2026, 10, 31, 0, 0)), '2026-10-30')

    def test_times_are_converted_to_beijing_and_elapsed_minutes_are_clear(self):
        self.assertEqual(MODULE.display_china_time('2026-10-23T09:21:40Z'), '2026-10-23 17:21:40')
        self.assertEqual(MODULE.display_china_time('2026-10-23T17:21:40+08:00'), '2026-10-23 17:21:40')
        self.assertEqual(MODULE.display_china_time(None), '未知')
        card = MODULE.build_health_card({'website_age_minutes': 61.6}, {}, datetime(2026, 10, 23, 22))
        rendered = json.dumps(card, ensure_ascii=False)
        self.assertIn('距上次采集完成：62 分钟', rendered)
        self.assertNotIn('61.6', rendered)

    def test_env_file_parses_export_without_evaluating_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'env'
            path.write_text('# ignored\nexport FEISHU_WEBHOOK_URL="https://example.test/a?x=1"\nPREVIEW_PASSWORD=\'literal$word\'\n')
            self.assertEqual(MODULE.env_file(path), {'FEISHU_WEBHOOK_URL': 'https://example.test/a?x=1', 'PREVIEW_PASSWORD': 'literal$word'})

    def test_card_displays_current_script_validation_failure(self):
        state={'website_fresh':True,'deploy_success':True,
            'podcast':{'status':'failed','failure_stage':'script_validation','date':'2026-11-05','online_verified':True}}
        card=MODULE.build_health_card(state,{},datetime(2026,11,6,23,30))
        self.assertIn('阻断阶段：稿件校验',json.dumps(card,ensure_ascii=False))
        self.assertEqual(card['header']['template'],'red')

    def test_card_displays_remote_verification_when_remote_is_stale(self):
        state = {'website_fresh': True, 'deploy_success': True,
                 'podcast': {'status': 'failed', 'failure_stage': 'remote_verification',
                             'date': '2026-11-15', 'online_verified': False}}
        rendered = json.dumps(MODULE.build_health_card(state, {}, datetime(2026, 11, 15, 23, 30)), ensure_ascii=False)
        self.assertIn('阶段：远端验证', rendered)
        self.assertIn('阻断阶段：远端验证', rendered)

    def test_missing_health_is_unknown(self):
        self.assertEqual(MODULE.read_json(Path('/nonexistent/newsradar-health')), {})

    def test_card_flags_podcast_failure_and_uses_signed_health_webhook(self):
        state = {'deploy_success': True, 'website_fresh': True,
                 'podcast': {'status': 'failed', 'online_verified': False}}
        card = MODULE.build_health_card(state, {}, datetime(2026, 10, 23, 22))
        self.assertEqual(card['schema'], '2.0')
        self.assertEqual(card['header']['template'], 'red')
        columns = card['body']['elements'][1]['columns']
        self.assertEqual(len(columns), 3)
        self.assertTrue(all(column['width'] == 'weighted' and column['weight'] == 1 for column in columns))
        self.assertEqual(card['body']['elements'][1]['horizontal_spacing'], '12px')
        config = {'NEWSRADAR_HEALTH_WEBHOOK_URL': 'https://open.larksuite.com/open-apis/bot/v2/hook/test', 'NEWSRADAR_HEALTH_WEBHOOK_SECRET': 'test-secret'}
        with patch.object(MODULE, 'env_file', return_value=config), patch.object(MODULE.time, 'time', return_value=123456), patch.object(MODULE.urllib.request, 'urlopen') as send:
            send.return_value.__enter__.return_value.read.return_value = b'{"code":0,"data":{}}'
            MODULE.send_health_card(card, 'health-test')
            request = send.call_args.args[0]
            self.assertEqual(request.full_url, config['NEWSRADAR_HEALTH_WEBHOOK_URL'])
            payload = json.loads(request.data)
            self.assertEqual(payload['msg_type'], 'interactive')
            self.assertEqual(payload['timestamp'], '123456')
            self.assertEqual(payload['sign'], base64.b64encode(hmac.new(b'123456\ntest-secret', b'', hashlib.sha256).digest()).decode())
            self.assertEqual(payload['card'], card)
