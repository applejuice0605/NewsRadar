import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
from filter_report_history import clean_report, Document


class HistoryFilterTests(unittest.TestCase):
    def test_removes_peripheral_nodes_and_repairs_counts_without_touching_script(self):
        source = '''<html><body><script>const x = "<div>keep script</div>"; const end = "</body>";</script>
        <button class="tab-btn" data-tab-index="all">全部<span class="tab-count">2</span></button>
        <button class="tab-btn" data-tab-index="0">推理<span class="tab-count">2</span></button>
        <div class="word-group" data-tab-index="0"><span class="word-count">2 条</span>
        <div class="news-item"><div class="news-number">1</div><a class="news-link">走私AI芯片</a></div>
        <div class="news-item"><div class="news-number">2</div><a class="news-link">vLLM推理新版本实测</a></div>
        </div></body></html>'''
        cleaned, count = clean_report(source)
        self.assertEqual(count, 1)
        self.assertNotIn('走私AI芯片', cleaned)
        self.assertIn('const x = "<div>keep script</div>";', cleaned)
        self.assertIn('const end = "</body>";</script>', cleaned)
        self.assertIn('class="word-count">1 条', cleaned)
        self.assertIn('class="tab-count">1', cleaned)
        self.assertIn('class="news-number">1', cleaned)
        self.assertEqual(clean_report(cleaned), (cleaned, 0))

    def test_empty_category_and_tab_removed(self):
        source = '<body><button class="tab-btn" data-tab-index="0">标签</button><div class="word-group" data-tab-index="0"><div class="news-item"><a class="news-link">抖音整治AI恶意营销号</a></div></div></body>'
        cleaned, count = clean_report(source)
        self.assertEqual(count, 1)
        self.assertNotIn('word-group', cleaned)
        self.assertNotIn('tab-btn', cleaned)
