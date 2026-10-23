import importlib.util
import tempfile
import unittest
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
