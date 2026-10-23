import importlib.util
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

    def test_card_flags_podcast_failure_and_routes_to_health_chat(self):
        state = {'deploy_success': True, 'website_fresh': True,
                 'podcast': {'status': 'failed', 'online_verified': False}}
        card = MODULE.build_health_card(state, {}, datetime(2026, 10, 23, 22))
        self.assertEqual(card['schema'], '2.0')
        self.assertEqual(card['header']['template'], 'red')
        with patch.object(MODULE.subprocess, 'run') as run:
            run.return_value.returncode = 0
            run.return_value.stdout = '{"ok":true,"data":{"message_id":"om_test"}}'
            self.assertEqual(MODULE.send_health_card(card, 'health-test')['message_id'], 'om_test')
            argv = run.call_args.args[0]
            self.assertEqual(argv[argv.index('--chat-id') + 1], 'oc_e09cc255d86e71d627ddcedd0510c01a')
            self.assertEqual(argv[argv.index('--msg-type') + 1], 'interactive')
