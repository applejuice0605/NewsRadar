import importlib.util
from pathlib import Path
import json
import tempfile
import unittest
spec=importlib.util.spec_from_file_location('audio_sync',Path(__file__).parents[1]/'scripts/sync_preview_audio.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


class PreviewAudioSyncTests(unittest.TestCase):
    def test_yesterdays_trial_never_overwrites_new_production_episode(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'trial';destination=Path(directory)/'site'
            source.mkdir();destination.mkdir()
            (source/'2026-10-29').mkdir();(source/'2026-10-29/daily.json').write_text('{"date":"2026-10-29"}')
            (source/'latest.json').write_text('{"date":"2026-10-29","preview_only":true}')
            (destination/'latest.json').write_text('{"date":"2026-10-30"}')
            self.assertFalse(m.sync(source,destination))
            self.assertEqual(json.loads((destination/'latest.json').read_text())['date'],'2026-10-30')
            self.assertTrue((destination/'2026-10-29/daily.json').exists())
            (source/'latest.json').write_text('{"date":"2026-10-30","preview_only":true}')
            self.assertTrue(m.sync(source,destination))

    def test_health_distinguishes_trial_audio_and_collector_generation(self):
        import sys
        sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
        from monitor_dashboard import matching_online_episode
        self.assertTrue(matching_online_episode({'date':'2026-10-29','preview_only':True},'2026-10-28','2026-10-28','2026-10-29'))
        self.assertFalse(matching_online_episode({'date':'2026-10-29'},'2026-10-28','2026-10-28','2026-10-29'))
        for value in ('2026-10-28abc','2026-10-30','invalid',None):
            self.assertFalse(matching_online_episode({'date':value,'preview_only':True},'2026-10-28','2026-10-28','2026-10-29'))
