"""Hermetic cross-consumer checks: no external model or TTS requests."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch

DEV=Path('/Users/bruce/Coding/NewsRadar')
sys.path[:0]=[str(DEV/'scripts'),str(DEV/'TrendRadar')]
from trendradar.ai.analyzer import AIAnalyzer
from trendradar.ai.daily_edition import freeze,podcast_payload,digest
import generate_dialogue_podcast as generator
import test_daily_edition as fixtures


class SharedFlowTest(unittest.TestCase):
    def edition(self,root):
        articles=fixtures.EditionTest().fixture(root)
        payload=json.loads(Path('/Users/bruce/Coding/NewsRadar-production/TrendRadar/output/audio/.daily-inputs/2026-11-02/editorial-materials.json').read_text())
        return freeze(root,datetime(2026,11,2,22,5,tzinfo=ZoneInfo('Asia/Shanghai')),articles=articles,
            builder=lambda *a,**kw:(payload,{}))

    def test_analysis_and_podcast_use_exact_projected_facts_and_cached_model_response(self):
        with tempfile.TemporaryDirectory() as temp:
            edition=self.edition(Path(temp));facts=edition['writer_plan']['topics'][0]['facts']
            self.assertEqual(digest(edition['writer_plan']),edition['facts_sha256'])
            calls=[]
            class Client:
                api_key='fixture'
                def __init__(self,*a,**kw):pass
                def validate_config(self):return True,''
                def chat(self,messages,**kw):
                    calls.append(messages)
                    return json.dumps({'evidence_claims':[{'section':'core_trends','fact':facts[0],
                        'source_ids':['N001'],'interpretation':'这是来源陈述。','uncertainty':'未独立复核。'}]},ensure_ascii=False)
            with patch('trendradar.ai.analyzer.AIClient',Client),patch('trendradar.ai.daily_edition.freeze',return_value=edition):
                for _ in range(2):
                    analyzer=AIAnalyzer({'MODEL':'fixture/model','NUM_RETRIES':9,'FALLBACK_MODELS':['fake']},
                        {'SHARED_DAILY_EDITION':True,'PRIMARY_MATERIALS':{'ENABLED':True}},
                        lambda:datetime(2026,11,2,22,5,tzinfo=ZoneInfo('Asia/Shanghai')))
                    result=analyzer.analyze([{'word':'wrong realtime','titles':[{'title':'wrong source'}]}])
                    self.assertTrue(result.success,result.error)
                    self.assertEqual(result.fact_pack['shared_edition']['facts_sha256'],edition['facts_sha256'])
            self.assertEqual(len(calls),1)
            prompt=calls[0][-1]['content']
            self.assertIn(facts[0],prompt);self.assertNotIn('wrong realtime',prompt)
            self.assertNotIn('wrong source',prompt)
            import podcast_dialogue
            self.assertEqual(podcast_dialogue.project_editorial_plan(podcast_payload(edition)['shared_editorial_plan'])[0],edition['writer_plan'])

    def test_writer_rejects_tampered_shared_payload_before_model_call(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);edition=self.edition(root);payload=podcast_payload(edition)
            payload['items'][0]['text']+=' 无依据内容。'
            materials=root/'tampered.json';materials.write_text(json.dumps(payload,ensure_ascii=False))
            prompt=root/'prompt.txt';prompt.write_text('{{FACT_BRIEF}}')
            args=generator.parse_args(['--date','2026-11-02','--materials',str(materials),'--prompt',str(prompt),
                '--output-root',str(root/'private-draft'),'--draft-only','--planned'])
            with patch.object(generator,'one_call_draft',side_effect=AssertionError('model called')):
                with self.assertRaisesRegex(ValueError,'differs from frozen artifact'):generator.generate(args)


if __name__=='__main__':unittest.main()
