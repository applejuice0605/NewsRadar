import importlib.util
from pathlib import Path
import unittest
ROOT=Path(__file__).parents[1]
spec=importlib.util.spec_from_file_location('material_merge',ROOT/'scripts/merge_preview_materials.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


class MergeTests(unittest.TestCase):
    def test_old_dates_do_not_overwrite_new_collection(self):
        current={'date':'2026-10-29','articles':[]}
        self.assertEqual(m.merge(current,{'date':'2026-10-28','articles':[{}]}),current)
    def test_changed_headline_at_same_url_cannot_receive_old_analysis(self):
        current={'date':'2026-10-29','articles':[{'id':'new','date':'2026-10-29','title':'新版本发布','url':'https://example.test/a'}]}
        material={'date':'2026-10-29','articles':[{'id':'old','title':'旧版本发布','url':'https://example.test/a','assessment':{'decision':'reject'}}],
                  'claims':[{'source_ids':['N001'],'fact':'旧版本发布'}],
                  'evidence_sources':{'N001':{'title':'旧版本发布','url':'https://example.test/a'}}}
        result=m.merge(current,material)
        self.assertEqual(result['claims'],[])
        self.assertNotIn('assessment',result['articles'][0])
    def test_matching_sources_share_assessment_and_claim(self):
        a={'id':'a','date':'2026-10-29','title':'模型新版本发布','url':'https://example.test/a','source':'项目'}
        current={'date':'2026-10-29','articles':[dict(a)]}
        material={'date':'2026-10-29','articles':[{**a,'assessment':{'decision':'accept','information_value':.8}}],
                  'claims':[{'source_ids':['N001'],'fact':a['title']}],'evidence_sources':{'N001':a}}
        result=m.merge(current,material)
        self.assertEqual(len(result['claims']),1)
        self.assertEqual(result['articles'][0]['assessment']['information_value'],.8)
