"""Archive badges reflect real per-date reports and playable episode assets."""
from __future__ import annotations
import importlib.util,json,tempfile,unittest,subprocess,os,sys
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('archive_under_test',ROOT/'scripts/generate_reports_archive.py')
a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)
class ArchiveAvailabilityTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.site=Path(self.tmp.name);a._report_profile.cache_clear()
  self.p=patch.multiple(a,SITE_DIR=self.site,REPORTS_DIR=self.site/'reports');self.p.start();self.addCleanup(self.p.stop)
 def report(self,day,*,ai=False,daily=True):
  p=self.site/'reports'/day/'report.html';p.parent.mkdir(parents=True,exist_ok=True)
  p.write_text(('全天汇总' if daily else '快照')+('<div class="ai-section-title">AI 热点分析</div><div>实际分析正文</div>' if ai else ''))
  return p
 def audio(self,day):
  d=self.site/'audio'/day/'edition';d.mkdir(parents=True)
  (d/'daily.mp3').write_bytes(b'complete mp3 asset')
  (d.parent/'daily.json').write_text(json.dumps({'date':day,'audio_url':f'/audio/{day}/edition/daily.mp3'}))
 def test_four_combinations_independent_per_day(self):
  for day,ai,audio in [('2026-10-25',False,False),('2026-10-26',True,False),('2026-10-27',False,True),('2026-10-28',True,True)]:
   self.report(day,ai=ai)
   if audio:self.audio(day)
  html=a._page_html(a._report_dates())
  import re
  for day,ai,audio in [('2026-10-25',False,False),('2026-10-26',True,False),('2026-10-27',False,True),('2026-10-28',True,True)]:
   card=re.search(r'<section class="archive-card" data-date="'+day+r'">(.*?)</section>',html,re.S).group(1)
   self.assertEqual('>AI 分析<' in card,ai)
   self.assertEqual('>AI 播客<' in card,audio)
 def test_analysis_in_snapshot_also_counts(self):
  self.report('2026-10-28',ai=True,daily=False)
  self.assertIn('>AI 分析<',a._page_html(a._report_dates()))
 def test_broken_missing_cross_date_external_and_escape_audio_not_advertised(self):
  day='2026-10-28';self.report(day);self.audio(day)
  p=self.site/'audio'/day/'daily.json';original=json.loads(p.read_text())
  for changed in [{'date':'2026-10-29'}, {'audio_url':'/audio/2026-10-27/edition/daily.mp3'}, {'audio_url':'https://elsewhere.test/a.mp3'}, {'audio_url':'/audio/2026-10-28/../../outside.mp3'}, {'audio_url':'/audio/2026-10-28/missing.mp3'}]:
   p.write_text(json.dumps({**original,**changed}));self.assertIsNone(a._podcast_url(day))
  p.write_text('{broken');self.assertIsNone(a._podcast_url(day))
  p.write_text(json.dumps(original));(self.site/'audio'/day/'edition/daily.mp3').write_bytes(b'')
  self.assertIsNone(a._podcast_url(day))
 def test_cli_honors_preview_and_existing_speech_visibility(self):
  self.report('2026-10-28',ai=True);self.audio('2026-10-28')
  env={k:v for k,v in os.environ.items() if k not in {'NEWSRADAR_FEATURE_SPEECH','NEWSRADAR_SITE_ENV'}}
  command=[sys.executable,str(ROOT/'scripts/generate_reports_archive.py'),'--site-dir',str(self.site)]
  subprocess.run(command,env=env,check=True,capture_output=True)
  self.assertNotIn('>AI 播客<',(self.site/'archive.html').read_text())
  subprocess.run(command+['--preview'],env=env,check=True,capture_output=True)
  self.assertIn('>AI 播客<',(self.site/'archive.html').read_text())
  subprocess.run(command+['--preview'],env={**env,'NEWSRADAR_FEATURE_SPEECH':'0'},check=True,capture_output=True)
  self.assertNotIn('>AI 播客<',(self.site/'archive.html').read_text())

 def test_audio_only_date_has_listening_link(self):
  self.audio('2026-10-28')
  self.assertIn('2026-10-28',a._report_dates())
  html=a._page_html(a._report_dates());self.assertIn('收听 AI 播客',html);self.assertNotIn('>AI 分析<',html)
if __name__=='__main__':unittest.main()
