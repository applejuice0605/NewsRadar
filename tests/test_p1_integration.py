"""Exercise the actual web/card/podcast contract, not isolated hand-made schemas."""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from datetime import date
ROOT=Path(__file__).parents[1]
sys.path.insert(0,str(ROOT/'TrendRadar'))
from trendradar.ai.fact_pack import make_fact_pack
from trendradar.ai.analyzer import AIAnalysisResult
from trendradar.ai.evidence import ground_analysis
from trendradar.ai.formatter import render_ai_analysis_html_rich,render_ai_analysis_feishu
spec=importlib.util.spec_from_file_location('p1_podcast',ROOT/'scripts/generate_daily_podcast.py')
podcast=importlib.util.module_from_spec(spec);spec.loader.exec_module(podcast)


class CanonicalIntegrationTests(unittest.TestCase):
    def pack(self):
        a={'id':'a','title':'模型v2.0发布新接口','source':'项目公告','url':'https://example.test/release','date':'2026-10-29','summary':None,'report_url':'/archive.html?date=2026-10-29'}
        sources={'N001':{**a,'summary':''}}
        claims=[{'section':'core_trends','fact':a['title'],'source_ids':['N001'],'interpretation':'可能影响接口回归测试','uncertainty':'来源陈述，未独立复测'}]
        return make_fact_pack([a],a['date'],claims,sources,generated_at='2026-10-29T08:00:00+08:00')
    def test_generated_pack_validates_in_podcast_and_is_shared_by_renderers(self):
        pack=self.pack()
        podcast.validate_fact_pack(pack,date(2026,10,29))
        result=ground_analysis(AIAnalysisResult(success=True,evidence_claims=pack['claims']),pack['evidence_sources']);result.fact_pack=pack
        page=render_ai_analysis_html_rich(result);card=render_ai_analysis_feishu(result)
        script,kind,_=podcast.script_from_fact_pack(pack)
        self.assertEqual(kind,'fact_pack_claims')
        for value in (page,card,script):
            # Markdown escapes punctuation, but the rendered fact is unchanged.
            self.assertIn('模型v2.0发布新接口',value.replace('\\.','.'))
            self.assertIn('可能影响接口回归测试',value)
        self.assertEqual(podcast.embedded_fact_pack(page),pack)
    def test_immutable_audio_bundle_retains_exact_canonical_material(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);stage=root/'stage';stage.mkdir()
            (stage/'daily.mp3').write_bytes(b'test-audio')
            (stage/'daily.txt').write_text('模型v2.0发布新接口')
            pack=self.pack();(stage/'facts.json').write_text(json.dumps(pack,ensure_ascii=False))
            metadata={'date':'2026-10-29'}
            podcast.publish_bundle(stage,root/'audio',metadata)
            saved=json.loads((root/'audio/latest.json').read_text())
            material=root/'audio'/saved['fact_pack_url'].removeprefix('/audio/')
            self.assertEqual(json.loads(material.read_text()),pack)
            self.assertEqual(material.parent,(root/'audio'/saved['audio_url'].removeprefix('/audio/')).parent)
