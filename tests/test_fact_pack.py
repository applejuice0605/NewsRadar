import json
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]/'TrendRadar'))
from trendradar.ai.fact_pack import annotate_articles,make_fact_pack,json_for_html
from trendradar.ai.analyzer import AIAnalysisResult
from trendradar.ai.evidence import ground_analysis
from trendradar.ai.formatter import render_ai_analysis_html_rich,render_ai_analysis_feishu


class FactPackTests(unittest.TestCase):
    def article(self,title,day='2026-10-28',host='example.test',id=None):
        return {'id':id or title,'title':title,'date':day,'url':'https://'+host+'/'+title,'source':host}
    def test_syndicated_titles_not_independent_or_removed(self):
        articles=[self.article('vLLM v1.0 发布',host='a.test'),self.article('vLLM v1.0 发布',host='b.test')]
        result,groups=annotate_articles(articles)
        self.assertEqual(len(result),2)
        self.assertEqual(result[0]['syndication']['group_id'],result[1]['syndication']['group_id'])
        self.assertFalse(result[1]['syndication']['independent_verified'])
        self.assertEqual(len(groups),1)
        self.assertIsNone(make_fact_pack(articles,'2026-10-28')['independent_source_count'])
    def test_new_versions_metrics_conclusions_remain_visible(self):
        titles=['vLLM v1.0 发布','vLLM v1.1 发布','vLLM 吞吐提高30%','vLLM 修复权限漏洞','vLLM 修复权限漏洞']
        result,_=annotate_articles([self.article(t,f'2026-10-{day:02}',id=str(day)) for day,t in enumerate(titles,24)])
        self.assertEqual([r['progress']['kind'] for r in result],['first_seen','new_version','new_metrics','new_conclusion_candidate','repeat_title'])
        self.assertEqual(len(result),5)
    def test_embedded_json_cannot_close_script(self):
        pack=make_fact_pack([self.article('</script><script>alert(1)</script>')],'2026-10-28')
        text=json_for_html(pack)
        self.assertNotIn('</script>',text)
        self.assertEqual(json.loads(text)['articles'][0]['title'],'</script><script>alert(1)</script>')
    def test_web_and_feishu_use_identical_pack_and_not_mutated_fields(self):
        sources={'N001':{'title':'vLLM新版本发布并修复问题','source':'原站','url':'https://example.test/release'}}
        claims=[{'section':'core_trends','fact':sources['N001']['title'],'source_ids':['N001'],'interpretation':'可能有助于部署','uncertainty':'需实际复测'}]
        pack=make_fact_pack([], '2026-10-28',claims,sources)
        result=ground_analysis(AIAnalysisResult(success=True,evidence_claims=claims),sources)
        result.fact_pack=pack;result.core_trends='不应流出的被篡改自由文本'
        page=render_ai_analysis_html_rich(result);card=render_ai_analysis_feishu(result)
        for output in (page,card):
            self.assertIn('vLLM新版本发布并修复问题',output)
            self.assertNotIn('不应流出的被篡改自由文本',output)
        self.assertIn('id="newsradar-fact-pack"',page)

    def test_old_version_reappearing_is_not_new_progress(self):
        rows=[self.article(t, f'2026-10-{day:02}', id=str(day)) for day,t in enumerate(
            ['vLLM v1.1 发布','vLLM v1.2 发布','vLLM v1.1 发布','vLLM v1.1 回顾'],25)]
        result,_=annotate_articles(rows)
        self.assertEqual(result[2]['progress']['kind'],'repeat_title')
        self.assertNotEqual(result[3]['progress']['kind'],'new_version')

    def test_version_punctuation_is_not_discarded_by_copy_fingerprint(self):
        rows=[self.article('vLLM v1.1 发布',id='a'),self.article('vLLM v11 发布',id='b')]
        result,_=annotate_articles(rows)
        self.assertNotEqual(result[0]['syndication']['group_id'],result[1]['syndication']['group_id'])
        self.assertEqual(result[1]['progress']['kind'],'new_version')
