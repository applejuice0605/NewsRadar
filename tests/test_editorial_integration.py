import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]/'TrendRadar'))
from trendradar.ai.filter_pipeline import AIFilterPipeline
from trendradar.core.loader import _load_ai_filter_config,_load_ai_analysis_config


class EditorialIntegrationTests(unittest.TestCase):
    def test_new_thresholds_and_bounded_primary_configuration_loaded(self):
        result=_load_ai_filter_config({'ai_filter':{'min_topic_relevance':.8,'min_information_value':.6}})
        self.assertEqual(result['MIN_TOPIC_RELEVANCE'],.8)
        self.assertEqual(result['MIN_INFORMATION_VALUE'],.6)
        self.assertFalse(_load_ai_analysis_config({})['PRIMARY_MATERIALS']['ENABLED'])
        self.assertTrue(_load_ai_analysis_config({'ai_analysis':{'primary_materials':{'enabled':True}}})['PRIMARY_MATERIALS']['ENABLED'])
    def test_complete_decisions_split_into_safe_output_budgets(self):
        class Filter:
            def __init__(self): self.counts=[]
            def classify_batch(self,titles,*args):
                self.counts.append(len(titles))
                self.last_assessments=[{'id':r['id'],'title':r['title'],'decision':'reject','completed':True} for r in titles]
                return []
        pipeline=object.__new__(AIFilterPipeline);classifier=Filter()
        rows=[{'id':i,'title':'标题'+str(i)} for i in range(50)]
        matches,completed,_=pipeline._classify_batches(classifier,rows,[],[],'',{'BATCH_SIZE':200,'BATCH_INTERVAL':0})
        self.assertEqual(classifier.counts,[24,24,2])
        self.assertEqual(completed,list(range(50)))
        self.assertEqual(matches,[])
