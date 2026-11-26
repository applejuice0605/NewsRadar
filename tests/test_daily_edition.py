import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime,date
from zoneinfo import ZoneInfo
from unittest.mock import patch

STAGE=Path(__file__).resolve().parents[1]
DEV=Path('/Users/bruce/Coding/NewsRadar')
sys.path[:0]=[str(DEV/'scripts'),str(DEV/'TrendRadar')]
spec=importlib.util.spec_from_file_location('daily_edition',STAGE/'TrendRadar/trendradar/ai/daily_edition.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
TZ=ZoneInfo('Asia/Shanghai')


class EditionTest(unittest.TestCase):
    def test_pre_cutoff_selection_replaces_sixth_with_seventh(self):
        day = '2026-11-18'
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            directory = repo / 'TrendRadar/output/meta/daily_editions' / day
            directory.mkdir(parents=True)
            candidates = [{'date': day, 'url': f'https://example.com/{i}'} for i in range(8)]
            lengths = [366, 899, 493, 522, 298, 381, 763]
            items = [{'url': candidate['url'], 'source_genre': 'source_report', 'text': '文' * length}
                     for candidate, length in zip(candidates, lengths)]
            payload = {'date': day, 'items': items}
            module.write_json(directory / 'candidates.json',
                              {'candidates': candidates, 'sha256': module.digest(candidates)})
            module.write_json(directory / 'source-capture.json', {
                'captured_at': day + 'T21:58:02+08:00', 'materials': payload,
                'materials_sha256': module.digest(payload)})
            plan = {'selection_report': []}
            with patch.object(module, 'ranked_candidates', side_effect=lambda candidates, _: candidates), \
                    patch('podcast_materials.validate_payload', return_value=(True, [])), \
                    patch('podcast_dialogue.project_editorial_plan', return_value=({}, '')):
                edition = module.freeze(repo, datetime(2026, 11, 18, 22, 5, tzinfo=TZ),
                                        planner=lambda _: plan)
            self.assertEqual(edition['materials']['items'], items[:5] + items[6:])
            self.assertEqual(edition['selection']['replacement'], {'removed_rank': 6, 'added_rank': 7})

    def fixture(self,repo):
        news=repo/'TrendRadar/output/news';rss=repo/'TrendRadar/output/rss'
        news.mkdir(parents=True);rss.mkdir(parents=True)
        with sqlite3.connect(news/'2026-11-02.db') as c:
            c.executescript('CREATE TABLE news_items(id INTEGER,first_crawl_time TEXT);CREATE TABLE rank_history(news_item_id INTEGER,rank INTEGER,crawl_time TEXT);CREATE TABLE ai_filter_tags(id INTEGER,tag TEXT);CREATE TABLE ai_filter_results(news_item_id INTEGER,source_type TEXT,tag_id INTEGER,relevance_score REAL,created_at TEXT,status TEXT);')
            c.executemany('INSERT INTO news_items VALUES(?,?)',[(1,'21-32'),(2,'22-01'),(3,'21-55'),(4,'unknown')])
            c.executemany('INSERT INTO rank_history VALUES(?,?,?)',[(1,9,'21-32'),(1,1,'22-30'),(2,1,'22-01'),(3,2,'21-55')])
            c.execute('INSERT INTO ai_filter_tags VALUES(1,"AI工程化")')
            c.executemany('INSERT INTO ai_filter_results VALUES(?,?,?,?,?,?)',[(i,'hotlist',1,.9,t,'active') for i,t in [(1,'2026-11-02 21:33:00'),(2,'2026-11-02 22:02:00'),(3,'2026-11-02 22:00:01'),(4,'2026-11-02 20:00:00')]])
        return [{'date':date(2026,11,2),'source_type':'hotlist','item_id':i,'title':f'新闻{i}','url':f'https://example.com/{i}','tags':[('AI工程化',.9)],'best_rank':1} for i in range(1,5)]

    def test_cutoff_first_crawl_classification_and_rank(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);articles=self.fixture(root)
            accepted=module.cutoff_candidates(root,'2026-11-02',articles)
            self.assertEqual([a['item_id'] for a in accepted],[1])
            self.assertEqual(accepted[0]['best_rank'],9)
            self.assertEqual(articles[0]['best_rank'],1)

    def test_wait_before_cutoff_and_freeze_reused_without_fetch(self):
        with tempfile.TemporaryDirectory() as temp:
            repo=Path(temp);articles=self.fixture(repo)
            with self.assertRaises(ValueError):module.freeze(repo,datetime(2026,11,2,21,59,tzinfo=TZ),articles=articles)
            payload=json.loads((Path('/Users/bruce/Coding/NewsRadar-production/TrendRadar/output/audio/.daily-inputs/2026-11-02/editorial-materials.json')).read_text())
            calls=[]
            def builder(*args,**kwargs):calls.append(kwargs['candidates']);return payload,{'ok':True}
            ed=module.freeze(repo,datetime(2026,11,2,22,5,tzinfo=TZ),articles=articles,builder=builder)
            reused=module.freeze(repo,datetime(2026,11,2,23,0,tzinfo=TZ),articles=[],builder=lambda *a,**k:self.fail('refetched'))
            self.assertEqual(ed,reused);self.assertEqual(len(calls),1)
            self.assertEqual(ed['news_cutoff_at'],'2026-11-02T22:00:00+08:00')
            materials=module.podcast_payload(ed)
            from podcast_dialogue import project_editorial_plan
            self.assertEqual(project_editorial_plan(materials['shared_editorial_plan'])[0],ed['writer_plan'])
            p=Path(ed['edition_path']);bad=json.loads(p.read_text());bad['writer_plan']['date']='2026-11-03';p.write_text(json.dumps(bad))
            with self.assertRaises(ValueError):module.load_edition(p)

    def test_invalid_selection_cannot_refresh_from_late_candidates(self):
        with tempfile.TemporaryDirectory() as temp:
            repo=Path(temp);articles=self.fixture(repo)
            def bad(*args,**kwargs):return {'date':'2026-11-02','items':[]},{}
            with self.assertRaises(ValueError):module.freeze(repo,datetime(2026,11,2,22,5,tzinfo=TZ),articles=articles,builder=bad)
            with self.assertRaisesRegex(ValueError,'no automatic reselection'):
                module.freeze(repo,datetime(2026,11,2,23,0,tzinfo=TZ),articles=[],builder=lambda *a,**k:self.fail('reselected'))

    def test_body_qualified_selection_reports_missing_candidates(self):
        with tempfile.TemporaryDirectory() as temp:
            repo=Path(temp);articles=self.fixture(repo)
            payload=json.loads(Path('/Users/bruce/Coding/NewsRadar-production/TrendRadar/output/audio/.daily-inputs/2026-11-02/editorial-materials.json').read_text())
            articles[0]['url']=payload['items'][0]['url']
            directory=repo/'TrendRadar/output/meta/daily_editions/2026-11-02';directory.mkdir(parents=True)
            module.write_json(directory/'source-capture.json',{'captured_at':'2026-11-02T21:58:50+08:00','materials':payload,'materials_sha256':module.digest(payload)})
            with patch('podcast_materials._is_candidate',return_value=(True,'candidate')):
                edition=module.freeze(repo,datetime(2026,11,2,22,5,tzinfo=TZ),articles=articles,settings={'min_material_items':1,'min_material_chars':100})
            self.assertEqual(len(edition['materials']['items']),1)
            self.assertEqual(edition['source_capture_at'],'2026-11-02T21:58:50+08:00')
        with tempfile.TemporaryDirectory() as temp:
            repo=Path(temp);articles=self.fixture(repo)
            payload=json.loads(Path('/Users/bruce/Coding/NewsRadar-production/TrendRadar/output/audio/.daily-inputs/2026-11-02/editorial-materials.json').read_text())
            articles[0]['url']=payload['items'][0]['url'];articles[1]['url']='https://example.com/late'
            with sqlite3.connect(repo/'TrendRadar/output/news/2026-11-02.db') as c:
                c.execute('UPDATE news_items SET first_crawl_time="21-59" WHERE id=2')
                c.execute('UPDATE rank_history SET crawl_time="21-59" WHERE news_item_id=2')
                c.execute('UPDATE ai_filter_results SET created_at="2026-11-02 21:59:10" WHERE news_item_id=2')
            directory=repo/'TrendRadar/output/meta/daily_editions/2026-11-02';directory.mkdir(parents=True)
            module.write_json(directory/'source-capture.json',{'captured_at':'2026-11-02T21:58:50+08:00','materials':payload,'materials_sha256':module.digest(payload)})
            with patch('podcast_materials._is_candidate',return_value=(True,'candidate')):
                edition=module.freeze(repo,datetime(2026,11,2,22,5,tzinfo=TZ),articles=articles,settings={'min_material_items':1,'min_material_chars':100})
            self.assertEqual(len(edition['materials']['items']),1)
            self.assertEqual(edition['selection']['excluded_candidates'][0]['url'],'https://example.com/late')
            self.assertEqual(edition['selection']['excluded_candidates'][0]['reason'],'no_qualified_body_captured_before_cutoff')

    def test_capture_nonempty_loop_and_deadline_preserves_previous_body(self):
        payload=json.loads(Path('/Users/bruce/Coding/NewsRadar-production/TrendRadar/output/audio/.daily-inputs/2026-11-02/editorial-materials.json').read_text())
        class Clock(datetime):
            current=datetime(2026,11,2,21,58,30,tzinfo=TZ)
            @classmethod
            def now(cls,tz=None):return cls.current
        with tempfile.TemporaryDirectory() as temp:
            repo=Path(temp)
            with patch.object(module,'datetime',Clock),patch('generate_site_insights.load_articles',return_value=([{}],[])),patch.object(module,'cutoff_candidates',return_value=[{}]),patch.object(module,'ranked_candidates',return_value=[{}]),patch('podcast_materials.build_materials',return_value=(payload,{})) as builder:
                path=module.capture_sources(repo,Clock.current)
                self.assertEqual(builder.call_count,1)
                original=path.read_bytes()
                Clock.current=datetime(2026,11,2,22,0,1,tzinfo=TZ)
                with self.assertRaisesRegex(ValueError,'crossed22:00'):
                    module.capture_sources(repo,datetime(2026,11,2,21,59,59,tzinfo=TZ))
                self.assertEqual(path.read_bytes(),original)

    def test_timestamp_date_and_timezone_are_checked(self):
        self.assertIsNone(module._day_seconds('2026-11-01 21:00:00','2026-11-02'))
        self.assertIsNone(module._day_seconds('2026-11-02T21:00:00+00:00','2026-11-02'))
        self.assertEqual(module._day_seconds('2026-11-02T13:59:00+00:00','2026-11-02'),21*3600+59*60)

    def test_analysis_response_reuse_and_failed_request_not_retried(self):
        with tempfile.TemporaryDirectory() as temp:
            ed={'edition_id':'abc','edition_path':str(Path(temp)/'edition.json')}
            class Client:
                calls=0
                def chat(self,messages,**kwargs):self.calls+=1;return 'raw'
            c=Client();self.assertEqual(module.analysis_once(ed,[{}],c),'raw')
            self.assertEqual(module.analysis_once(ed,[{'changed':True}],c),'raw');self.assertEqual(c.calls,1)
        with tempfile.TemporaryDirectory() as temp:
            ed={'edition_id':'abc','edition_path':str(Path(temp)/'edition.json')}
            class Failed:
                def chat(self,*a,**k):raise RuntimeError('connection')
            with self.assertRaises(RuntimeError):module.analysis_once(ed,[],Failed())
            with self.assertRaisesRegex(ValueError,'already attempted'):module.analysis_once(ed,[],Failed())


if __name__=='__main__':unittest.main()
