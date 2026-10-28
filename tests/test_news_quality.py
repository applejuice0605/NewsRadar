import importlib.util
import unittest
from datetime import date
from pathlib import Path
import sys

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from generate_site_insights import peripheral_reason, build_quality_metrics


class NewsQualityTests(unittest.TestCase):
    def test_rejects_reported_peripheral_examples(self):
        titles = [
            '马来西亚查获价值4950万林吉特走私AI芯片，30台服务器被扣',
            '豆包公关负责人刘星回应横店与字节合作将15万群演肖像声音喂给大模型：传言严重失实',
            '抖音整治AI恶意营销号，近一月处置违规账号50.7万个',
        ]
        self.assertTrue(all(peripheral_reason(title) for title in titles))

    def test_preserves_engineering_and_security_reporting(self):
        titles = ['广告推荐模型推理优化：实测延迟降低30%',
                  'OpenCode密钥泄露漏洞修复：权限隔离实践',
                  'vLLM发布新版本，推理吞吐提升20%',
                  'Agent自动化测试评估数据集开源']
        self.assertTrue(all(peripheral_reason(title) is None for title in titles))

    def test_metrics_deduplicate_and_distinguish_yield_from_model_score(self):
        day = date(2026, 10, 28)
        articles = [
            {'date': day, 'title': 'vLLM新版本', 'tags': [('推理', .9)]},
            {'date': day, 'title': 'vLLM 新版本', 'tags': [('推理', .8)]},
            {'date': day, 'title': '走私AI芯片', 'tags': [('推理', .95)]},
            {'date': day, 'title': '足球新闻', 'tags': []},
        ]
        result = build_quality_metrics(articles, day)
        self.assertEqual(result['collected_count'], 3)
        self.assertEqual(result['accepted_count'], 1)
        self.assertEqual(result['mean_relevance_percent'], 90)
        self.assertEqual(result['selection_rate_percent'], 33.3)
        self.assertEqual(result['peripheral_count'], 1)
        self.assertIsNone(build_quality_metrics([], day)['mean_relevance_percent'])

    def test_existing_cached_classifications_are_filtered(self):
        from trendradar.ai.filter_pipeline import AIFilterPipeline
        pipeline = object.__new__(AIFilterPipeline)
        pipeline._priority_sort_enabled = False
        result = pipeline._build_filter_result([
            {'tag': '推理', 'title': '走私AI芯片', 'relevance_score': .99},
            {'tag': '推理', 'title': 'vLLM推理吞吐优化实测', 'relevance_score': .9},
        ], [{'tag': '推理'}], 2)
        self.assertEqual([item['title'] for group in result.tags for item in group['items']], ['vLLM推理吞吐优化实测'])
