import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trendradar.notification.dedup import DeliveryLedger
from trendradar.notification.dispatcher import NotificationDispatcher


class DeliveryDedupTests(unittest.TestCase):
    def payload(self):
        return {'stats': [{'word': '模型', 'titles': [
            {'title': '模型 v1 吞吐提高20%', 'source_name': '甲'},
            {'title': '模型 v1 吞吐提高20%', 'source_name': '乙'},
            {'title': '模型 v2 吞吐提高30%', 'source_name': '乙'},
        ]}], 'new_titles': [], 'total_new_count': 3}

    def test_cross_source_duplicates_and_updates_and_account_isolation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'ledger.db'
            ledger = DeliveryLedger(path, 'account-a')
            report, _, _, selected, metrics = ledger.filter_payload(self.payload(), None, None)
            self.assertEqual(len(selected), 2)
            self.assertEqual(metrics['cross_source_duplicate_count'], 1)
            self.assertEqual(metrics['delivered_blocked_count'], 0)
            ledger.record_success(selected)
            _, _, _, selected, metrics = ledger.filter_payload(self.payload(), None, None)
            self.assertEqual(selected, [])
            self.assertEqual(metrics['delivered_blocked_count'], 2)
            other = DeliveryLedger(path, 'account-b')
            self.assertEqual(len(other.filter_payload(self.payload(), None, None)[3]), 2)
            self.assertEqual(len(ledger.filter_payload(self.payload(), None, None, block_delivered=False)[3]), 2)
            other.close()
            ledger.close()

    def test_failed_delivery_retries_and_successful_delivery_blocks(self):
        with tempfile.TemporaryDirectory() as temporary:
            dispatcher = NotificationDispatcher({'FEISHU_WEBHOOK_URL': 'https://example.test/hook', 'STORAGE': {'LOCAL': {'DATA_DIR': temporary}}}, lambda: None, lambda *a: [])
            args = (self.payload(), '增量分析', None, None, 'incremental')
            with patch('trendradar.notification.dispatcher.send_to_feishu', return_value=False) as sender:
                self.assertFalse(dispatcher._send_feishu(*args))
                self.assertFalse(dispatcher._send_feishu(*args))
                self.assertEqual(sender.call_count, 2)
            with patch('trendradar.notification.dispatcher.send_to_feishu', return_value=True) as sender:
                self.assertTrue(dispatcher._send_feishu(*args))
                self.assertFalse(dispatcher._send_feishu(*args))
                self.assertEqual(sender.call_count, 1)
