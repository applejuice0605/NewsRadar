import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
from evaluate_news_quality import evaluate


class EvaluationTests(unittest.TestCase):
    def test_rules_do_not_claim_ai_accuracy(self):
        data = [{'id': '1', 'title': '足球比赛赛程', 'expected_peripheral': False, 'expected_accept': False, 'expected_tag': None}]
        result = evaluate(data)
        self.assertEqual(result['rule_pass_count'], 1)
        self.assertIsNone(result['ai_evaluation'])

    def test_false_positive_and_false_negative_measured_separately(self):
        data = [{'id': '1', 'title': 'AI工具版本', 'expected_peripheral': False, 'expected_accept': True, 'expected_tag': '工程'},
                {'id': '2', 'title': '足球赛程', 'expected_peripheral': False, 'expected_accept': False, 'expected_tag': None}]
        result = evaluate(data, [{'id': '1', 'accepted': False}, {'id': '2', 'accepted': True, 'tag': '工程'}])['ai_evaluation']
        self.assertEqual((result['fp'], result['fn']), (1, 1))
        self.assertEqual(result['precision'], 0)
        self.assertEqual(result['recall'], 0)
        with self.assertRaises(ValueError):
            evaluate(data, [{'id': '1', 'accepted': True}])
