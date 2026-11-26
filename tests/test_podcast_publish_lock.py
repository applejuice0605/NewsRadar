"""Exercise the real publication lock without a site, Git or network calls."""
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PUBLISHER = Path(__file__).resolve().parents[1]/'scripts/publish_reports_site.sh'

def prepare_project(root):
    project = root / "TrendRadar"
    project.mkdir()
    (project / ".venv").symlink_to(Path(sys.prefix), target_is_directory=True)
    source = PUBLISHER.parents[1] / "TrendRadar/trendradar"
    (project / "trendradar").symlink_to(source, target_is_directory=True)
    scripts = root / "scripts"
    scripts.mkdir()
    (scripts / "publish_daily_editions.py").symlink_to(PUBLISHER.with_name("publish_daily_editions.py"))
    return project


class PublisherLockTest(unittest.TestCase):
    def test_busy_publisher_defers_then_lock_file_can_be_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = prepare_project(root) / 'output/logs/.site-publish.lock'
            lock_path.parent.mkdir(parents=True)
            env = {**os.environ, 'REPO_DIR': str(root), 'NEWSRADAR_SITE_GIT_PUSH': '0'}
            env.pop('NEWSRADAR_SITE_PUBLISH_LOCK_HELD', None)
            with lock_path.open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                busy = subprocess.run([str(PUBLISHER)], env=env, text=True, capture_output=True, timeout=10)
                self.assertEqual(busy.returncode, 75, busy.stderr)
            # Missing index returns before any Git/network operation. The
            # persistent lock file remains, but no stale directory blocks it.
            for _ in range(2):
                free = subprocess.run([str(PUBLISHER)], env=env, text=True, capture_output=True, timeout=10)
                self.assertEqual(free.returncode, 0, free.stderr)

    def test_audio_sync_excludes_writer_cache_worker_state_and_drafts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = prepare_project(root)
            (project/'output/html/latest').mkdir(parents=True)
            (project/'output/index.html').write_text('<html><body>fixture</body></html>')
            (project/'output/html/latest/daily.html').write_text('<html>daily</html>')
            audio = project/'output/audio'
            (audio/'2026-10-30/episode').mkdir(parents=True)
            (audio/'latest.json').write_text('{"date":"2026-10-30"}')
            (audio/'2026-10-30/episode/daily.mp3').write_bytes(b'fixture-mp3')
            (audio/'.editorial-cache').mkdir()
            (audio/'.editorial-cache/private.json').write_text('private fixture')
            (audio/'.daily-worker-state.json').write_text('private state')
            (audio/'drafts').mkdir()
            (audio/'drafts/script.txt').write_text('unpublished draft')
            (root/'scripts/generate_reports_archive.py').write_text('pass\n')
            env = {**os.environ, 'REPO_DIR':str(root), 'NEWSRADAR_SITE_GIT_PUSH':'0', 'NEWSRADAR_SITE_APPLY_FEATURES':'0'}
            env.pop('NEWSRADAR_SITE_PUBLISH_LOCK_HELD', None)
            result = subprocess.run([str(PUBLISHER)],env=env,text=True,capture_output=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            public = root/'docs/audio'
            self.assertTrue((public/'latest.json').is_file())
            self.assertTrue((public/'2026-10-30/episode/daily.mp3').is_file())
            self.assertFalse((public/'.editorial-cache').exists())
            self.assertFalse((public/'.daily-worker-state.json').exists())
            self.assertFalse((public/'drafts').exists())
