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
    def test_env_file_parses_export_without_evaluating_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'env'
            path.write_text('# ignored\nexport FEISHU_WEBHOOK_URL="https://example.test/a?x=1"\nPREVIEW_PASSWORD=\'literal$word\'\n')
            self.assertEqual(MODULE.env_file(path), {'FEISHU_WEBHOOK_URL': 'https://example.test/a?x=1', 'PREVIEW_PASSWORD': 'literal$word'})

    def test_missing_health_is_unknown(self):
        self.assertEqual(MODULE.read_json(Path('/nonexistent/newsradar-health')), {})

    def test_card_flags_podcast_failure_and_uses_signed_health_webhook(self):
        state = {'deploy_success': True, 'website_fresh': True,
                 'podcast': {'status': 'failed', 'online_verified': False}}
        card = MODULE.build_health_card(state, {}, datetime(2026, 10, 23, 22))
        self.assertEqual(card['schema'], '2.0')
        self.assertEqual(card['header']['template'], 'red')
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
