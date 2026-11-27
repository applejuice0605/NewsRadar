import json
import os
import unittest
from datetime import datetime
from unittest.mock import patch

os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
from trendradar.ai.filter import AIFilter


class TokenBudgetTests(unittest.TestCase):
    def run_classification(self, finishes):
        with patch('trendradar.ai.filter.AIClient') as client_class:
            client = client_class.return_value
            def respond(*args, **kwargs):
                client.last_response_metadata = {'finish_reason': next(finishes)}
                return json.dumps([{'id': 1, 'tag_id': None, 'topic_relevance': 0,
                    'information_value': 0, 'decision': 'reject',
                    'reason': '汽车产品新闻', 'evidence_quote': ''}])
            client.chat.side_effect = respond
            engine = AIFilter({}, {'PROMPT_FILE': 'prompt.txt'}, datetime.now)
            result = engine.classify_batch([{'id': 1, 'title': '汽车新品发布'}],
                [{'id': 1, 'tag': '大厂战略'}])
            return engine, client, result

    def test_truncation_retries_with_larger_budget(self):
        engine, client, result = self.run_classification(iter(['length', 'stop']))
        self.assertEqual(result, [])
        self.assertTrue(engine.last_assessments[0]['completed'])
        self.assertEqual([c.kwargs['max_tokens'] for c in client.chat.call_args_list], [20000, 32000])

    def test_repeated_truncation_does_not_persist_partial_decisions(self):
        engine, client, result = self.run_classification(iter(['length', 'length']))
        self.assertIsNone(result)
        self.assertFalse(engine.last_assessments[0]['completed'])
        self.assertEqual(client.chat.call_count, 2)

    def test_complete_response_uses_one_request(self):
        engine, client, result = self.run_classification(iter(['stop']))
        self.assertEqual(result, [])
        self.assertEqual(client.chat.call_count, 1)
