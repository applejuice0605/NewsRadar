"""AIClient response metadata stays bounded and chat-compatible."""
from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "TrendRadar"))

from trendradar.ai.client import AIClient


def response(content, *, finish_reason=None, model=None, usage=None):
    choice = SimpleNamespace(message=SimpleNamespace(content=content))
    if finish_reason is not None:
        choice.finish_reason = finish_reason
    result = SimpleNamespace(choices=[choice])
    if model is not None:
        result.model = model
    if usage is not None:
        result.usage = usage
    return result


class AIClientMetadataTests(unittest.TestCase):
    def client(self):
        return AIClient({"MODEL": "fixture/model", "API_KEY": "secret", "NUM_RETRIES": 0})

    def test_string_response_remains_compatible(self):
        client = self.client()
        with patch("trendradar.ai.client.completion", return_value=response("hello")):
            self.assertEqual(client.chat([{"role": "user", "content": "hi"}]), "hello")

    def test_content_blocks_remain_flattened(self):
        client = self.client()
        blocks = [{"text": "first"}, {"kind": "other"}, "last"]
        with patch("trendradar.ai.client.completion", return_value=response(blocks)):
            self.assertEqual(client.chat([]), "first\n{'kind': 'other'}\nlast")

    def test_finish_reason_model_usage_and_reasoning_tokens_are_recorded(self):
        client = self.client()
        usage = SimpleNamespace(
            prompt_tokens=11,
            completion_tokens=7,
            total_tokens=18,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=3),
        )
        with patch(
            "trendradar.ai.client.completion",
            side_effect=[response("cut", finish_reason="length", model="fixture/model", usage=usage),
                         response("done", finish_reason="stop")],
        ):
            self.assertEqual(client.chat([]), "cut")
            self.assertEqual(
                client.last_response_metadata,
                {
                    "finish_reason": "length",
                    "model": "fixture/model",
                    "usage": {
                        "prompt_tokens": 11,
                        "completion_tokens": 7,
                        "total_tokens": 18,
                        "reasoning_tokens": 3,
                    },
                },
            )
            self.assertEqual(client.chat([]), "done")
            self.assertEqual(client.last_response_metadata, {"finish_reason": "stop"})

    def test_missing_metadata_is_normal(self):
        client = self.client()
        with patch("trendradar.ai.client.completion", return_value=response("plain")):
            self.assertEqual(client.chat([]), "plain")
            self.assertEqual(client.last_response_metadata, {})

    def test_failed_request_clears_old_metadata_and_is_not_retried(self):
        client = self.client()
        completion = patch(
            "trendradar.ai.client.completion",
            side_effect=[response("ok", finish_reason="stop"), RuntimeError("boom")],
        )
        with completion as mocked:
            client.chat([])
            with self.assertRaisesRegex(RuntimeError, "boom"):
                client.chat([])
            self.assertEqual(mocked.call_count, 2)
            self.assertEqual(client.last_response_metadata, {})

    def test_metadata_does_not_capture_request_secrets_or_endpoint(self):
        client = AIClient(
            {
                "MODEL": "fixture/model",
                "API_KEY": "request-secret",
                "API_BASE": "https://private.example/v1",
                "NUM_RETRIES": 0,
            }
        )
        with patch(
            "trendradar.ai.client.completion",
            return_value=response("ok", finish_reason="stop", model="fixture/model"),
        ):
            client.chat([])
        self.assertEqual(client.last_response_metadata, {"finish_reason": "stop", "model": "fixture/model"})
        self.assertNotIn("api_key", client.last_response_metadata)
        self.assertNotIn("endpoint", client.last_response_metadata)
        self.assertNotIn("api_base", client.last_response_metadata)


if __name__ == "__main__":
    unittest.main()
