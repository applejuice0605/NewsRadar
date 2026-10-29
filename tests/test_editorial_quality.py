import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

# Keep imports offline: LiteLLM otherwise fetches a cost map at import time.
os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'TrendRadar'))
sys.path.insert(0, str(ROOT / 'scripts'))
from trendradar.ai.editorial_quality import parse_decisions, score, write_sidecar
from trendradar.ai.filter import AIFilter
from trendradar.ai.filter_pipeline import AIFilterPipeline
from trendradar.storage.local import LocalStorageBackend
from evaluate_filter_live import select_real_samples, evaluate_response, requests, TAG_NAMES, main
from evaluate_news_quality import evaluate

TITLES = [dict(id=1, title='广告模型发布：推理吞吐提升20%'), dict(id=2, title='AI好可怕')]
TAGS = [dict(id=7, tag='推理加速')]


def decision(i=1, **changes):
    row = dict(id=i, tag_id=7, topic_relevance=.9, information_value=.8,
               decision='accept', reason='具体推理性能变化', evidence_quote='推理吞吐提升20%')
    row.update(changes)
    return row


def bare_filter():
    f = object.__new__(AIFilter)
    f.filter_config = {}
    f.debug = False
    f.classify_system = 'system'
    f.classify_user = '{news_list} {min_topic_relevance} {min_information_value}'
    f.client = Mock()
    return f


class EditorialParserTests(unittest.TestCase):
    def test_independent_scores_and_every_input(self):
        result = parse_decisions([decision(), decision(2, information_value=.2,
            evidence_quote='', decision='reject', reason='无信息增量')], TITLES, TAGS)
        self.assertEqual([a['decision'] for a in result], ['accept', 'reject'])
        self.assertEqual(result[1]['topic_relevance'], .9)
        self.assertEqual(result[1]['information_value'], .2)
        self.assertTrue(all(a['completed'] for a in result))

    def test_two_thresholds_are_configurable(self):
        for changes, code in [({'information_value': .49}, 'insufficient_information_value'),
                              ({'topic_relevance': .69}, 'insufficient_topic_relevance')]:
            a = parse_decisions([decision(**changes)], TITLES[:1], TAGS)[0]
            self.assertEqual((a['decision'], a['reason_code']), ('reject', code))
        a = parse_decisions([decision(topic_relevance=.65, information_value=.4)], TITLES[:1], TAGS,
                            {'MIN_TOPIC_RELEVANCE': .6, 'MIN_INFORMATION_VALUE': .4})[0]
        self.assertEqual(a['decision'], 'accept')

    def test_scores_are_strict_bounded_finite(self):
        for bad in [True, False, '0.9', None, [], {}, float('nan'), float('inf'), -.1, 1.1]:
            for key in ['topic_relevance', 'information_value']:
                with self.subTest(bad=bad, key=key):
                    a = parse_decisions([decision(**{key: bad})], TITLES[:1], TAGS)[0]
                    self.assertFalse(a['completed'])
                    self.assertEqual(a['status'], 'request_failure')
        self.assertEqual(score(0), 0)
        self.assertEqual(score(1), 1)

    def test_invalid_ids_cannot_alias_integers_or_crash(self):
        for bad in [True, 1.0, '1', [], {}]:
            a = parse_decisions([decision(id=bad)], TITLES[:1], TAGS)[0]
            self.assertFalse(a['completed'])
        for bad in [True, 7.0, '7', [], {}, 99]:
            a = parse_decisions([decision(tag_id=bad)], TITLES[:1], TAGS)[0]
            self.assertFalse(a['completed'])

    def test_value_evidence_must_quote_title(self):
        for quote in ['性能提升50%', 'source:推理吞吐提升20%', '', '   ', None]:
            a = parse_decisions([decision(evidence_quote=quote)], TITLES[:1], TAGS)[0]
            self.assertFalse(a['completed'])
        a = parse_decisions([decision(decision='reject', information_value=.2, evidence_quote='')], TITLES[:1], TAGS)[0]
        self.assertEqual(a['decision'], 'reject')
        self.assertTrue(a['completed'])

    def test_schema_errors_and_duplicate_do_not_become_rejections(self):
        for response in [None, {}, '[]', [decision(), decision()], [dict(id=1, decision='accept')],
                         [decision(decision=True)], [decision(reason='')], [decision(tag_id=None)]]:
            a = parse_decisions(response, TITLES[:1], TAGS)[0]
            self.assertEqual((a['status'], a['decision']), ('request_failure', None))
        a = parse_decisions([decision(decision='reject', tag_id=None)], TITLES[:1], TAGS)[0]
        self.assertTrue(a['completed'])

    def test_legacy_flat_nested_and_empty_arrays(self):
        for data in [[{'id': 1, 'tag_id': 7, 'score': .8}],
                     [{'id': 1, 'tags': [{'tag_id': 7, 'score': .8}]}]]:
            a, missing = parse_decisions(data, TITLES, TAGS)
            self.assertEqual((a['decision'], a['status']), ('accept', 'legacy_unknown'))
            self.assertIsNone(a['information_value'])
            self.assertEqual(a['evidence_quote'], '')
            self.assertFalse(missing['completed'])
        a = parse_decisions([{'id': 1, 'tags': []}], TITLES[:1], TAGS)[0]
        self.assertEqual((a['decision'], a['status']), ('reject', 'legacy_unknown'))
        self.assertFalse(parse_decisions([], TITLES[:1], TAGS)[0]['completed'])

    def test_legacy_invalid_scores_are_not_fabricated(self):
        for bad in [True, 'bad', float('nan'), 2]:
            a = parse_decisions([dict(id=1, tag_id=7, score=bad)], TITLES[:1], TAGS)[0]
            self.assertFalse(a['completed'])

    def test_batch_failure_and_partial_completion(self):
        f = bare_filter()
        for response in ['', 'not json', '{}', '[', '[]']:
            f.client.chat.return_value = response
            self.assertIsNone(f.classify_batch(TITLES, TAGS))
            self.assertEqual(len(f.last_assessments), 2)
            self.assertTrue(all(not a['completed'] for a in f.last_assessments))
        f.client.chat.return_value = json.dumps([decision()])
        matched = f.classify_batch(TITLES, TAGS)
        self.assertEqual([r['news_item_id'] for r in matched], [1])
        self.assertIn('assessment', matched[0])
        self.assertFalse(f.last_assessments[1]['completed'])
        f.client.chat.side_effect = RuntimeError('do not persist exception details')
        self.assertIsNone(f.classify_batch(TITLES, TAGS))
        self.assertNotIn('do not persist', f.last_assessments[0]['reason'])

    def test_thresholds_render_in_both_prompt_sections(self):
        f = bare_filter()
        f.filter_config = {'MIN_TOPIC_RELEVANCE': .8, 'MIN_INFORMATION_VALUE': .6}
        f.classify_system = 'topic={min_topic_relevance};value={min_information_value}'
        f.client.chat.return_value = json.dumps([decision()])
        f.classify_batch(TITLES[:1], TAGS)
        messages = f.client.chat.call_args.args[0]
        self.assertEqual(messages[0]['content'], 'topic=0.8;value=0.6')
        self.assertIn('0.8 0.6', messages[1]['content'])

    def test_reject_batch_succeeds_and_no_keyword_veto(self):
        f = bare_filter()
        f.client.chat.return_value = json.dumps([decision(decision='reject', information_value=.2)])
        self.assertEqual(f.classify_batch(TITLES[:1], TAGS), [])
        self.assertTrue(f.last_assessments[0]['completed'])
        f.client.chat.return_value = json.dumps([decision()])
        self.assertEqual(len(f.classify_batch(TITLES[:1], TAGS)), 1)


class PipelineQualityTests(unittest.TestCase):
    def pipeline(self):
        p = object.__new__(AIFilterPipeline)
        p._debug = False
        p._priority_sort_enabled = False
        p.get_time = lambda: datetime(2026, 10, 29, 12)
        return p

    def test_only_completed_ids_marked_and_sources_separate(self):
        p, f = self.pipeline(), bare_filter()
        f.client.chat.side_effect = [json.dumps([decision()]), json.dumps([
            decision(1, decision='reject', information_value=.2)])]
        matched, news, rss = p._classify_batches(f, TITLES, TITLES[:1], TAGS, '', {'BATCH_INTERVAL': 0})
        self.assertEqual((news, rss), ([1], [1]))
        self.assertEqual(len(p._run_assessments), 3)
        self.assertEqual(p._run_assessments[-1]['source_type'], 'rss')
        p.storage = Mock()
        p._save_results(matched, news, rss, 'interests.txt', 'hash')
        self.assertEqual(p.storage.save_analyzed_news.call_args_list[0].args[0], [1])
        self.assertEqual(p.storage.save_analyzed_news.call_args_list[1].args[-1], set())

    def test_failed_batch_never_marked_analyzed(self):
        p, f = self.pipeline(), bare_filter()
        f.client.chat.return_value = 'not json'
        matched, news, rss = p._classify_batches(f, TITLES, [], TAGS, '', {'BATCH_INTERVAL': 0})
        self.assertEqual((matched, news, rss), ([], [], []))
        p.storage = Mock()
        p._save_results(matched, news, rss, 'i', 'h')
        p.storage.save_analyzed_news.assert_not_called()

    def test_sidecar_roundtrip_append_and_corruption_is_nonfatal(self):
        with tempfile.TemporaryDirectory() as directory:
            p, f = self.pipeline(), bare_filter()
            backend = LocalStorageBackend(data_dir=directory, enable_txt=False, enable_html=False)
            p.storage = SimpleNamespace(_backend=backend)
            p._run_assessments = [dict(parse_decisions([decision()], TITLES[:1], TAGS)[0], source_type='hotlist')]
            p._record_quality('i', f)
            path = Path(p._quality_sidecar_path)
            self.assertEqual(path, Path(directory) / 'meta/filter_quality/2026-10-29.json')
            p._record_quality('i', f)
            document = json.loads(path.read_text())
            self.assertEqual(len(document['runs']), 2)
            self.assertEqual(document['runs'][0]['policy_version'], 'editorial-v2')
            p._run_assessments = []
            p._record_quality('i', f)
            self.assertEqual(p._quality_by_id[('hotlist', 1)]['information_value'], .8)
            path.write_text('{invalid')
            p._record_quality('i', f)
            self.assertTrue(p._quality_notes)
            self.assertEqual(path.read_text(), '{invalid')

    def test_remote_never_guesses_private_path(self):
        p, f = self.pipeline(), bare_filter()
        p.storage = SimpleNamespace(_backend=SimpleNamespace(data_dir='/private/db'))
        p._run_assessments = [dict(parse_decisions([decision()], TITLES[:1], TAGS)[0], source_type='hotlist')]
        with patch('trendradar.ai.filter_pipeline.write_sidecar') as writer:
            p._record_quality('i', f)
            writer.assert_not_called()
        self.assertTrue(p._quality_notes)
        result = p._build_filter_result([dict(news_item_id=1, title=TITLES[0]['title'], tag='推理', relevance_score=.9)], TAGS, 1)
        self.assertEqual(result.tags[0]['items'][0]['assessment']['information_value'], .8)
        self.assertEqual(len(result.assessments), 1)

    def test_old_cache_has_explicit_unknown_not_fabricated_value(self):
        p = self.pipeline()
        result = p._build_filter_result([dict(title='模型新功能', tag='模型', relevance_score=.9)], TAGS, 0)
        assessment = result.tags[0]['items'][0]['assessment']
        self.assertEqual(assessment['policy_version'], 'legacy_unknown')
        self.assertIsNone(assessment['information_value'])
        self.assertEqual(assessment['evidence_quote'], '')


class RealEvaluationTests(unittest.TestCase):
    def test_fixture_provenance_balance_and_no_ai_truth(self):
        fixture = ROOT / 'tests/fixtures/news_quality_real.jsonl'
        labelled = [json.loads(line) for line in fixture.read_text().splitlines()]
        # Keep CI independent of the user's private DB; the real CLI verifies
        # these exact keys/titles read-only before it prepares or runs any request.
        articles = [dict(key=s['real_article_key'], title=s['title']) for s in labelled]
        with patch('evaluate_filter_live.load_articles', return_value=(articles, [])):
            samples, skipped = select_real_samples(Path('/unused'), fixture)
        with patch('evaluate_filter_live.load_articles', return_value=([], [])):
            with self.assertRaises(ValueError):
                select_real_samples(Path('/unused'), fixture)
        self.assertEqual(len(samples), 24)
        self.assertEqual(sum(s['expected_accept'] for s in samples), 12)
        self.assertTrue(all(not s['human_reviewed'] for s in samples))
        self.assertTrue(all(s['annotation_kind'] == 'editorial_manual' for s in samples))

    def test_fp_fn_and_tag_accuracy_independent_of_scores(self):
        samples = [dict(id=1, title='a', expected_accept=True, expected_tag='x', expected_peripheral=False),
                   dict(id=2, title='b', expected_accept=False, expected_tag=None, expected_peripheral=False)]
        metric = evaluate(samples, [dict(id=1, accepted=False), dict(id=2, accepted=True, tag='x')])['ai_evaluation']
        self.assertEqual((metric['fp'], metric['fn']), (1, 1))
        self.assertEqual(metric['false_positive_ids'], [2])
        self.assertEqual(metric['false_negative_ids'], [1])

    def test_replay_failures_and_old_sparse_caveat(self):
        samples = [dict(TITLES[0], expected_accept=True, expected_tag='推理加速', expected_peripheral=False)]
        failed = evaluate_response(samples, TAGS, '{bad', 'new')
        self.assertEqual(failed['failure_count'], 1)
        self.assertIsNone(failed['metrics']['precision'])
        self.assertEqual(failed['metrics']['fn'], 0)
        old = evaluate_response(samples, TAGS, '[]', 'old')
        self.assertEqual(old['metrics']['fn'], 1)
        self.assertTrue(old['legacy_omission_caveat'])
        new = evaluate_response(samples, TAGS, json.dumps([decision()]), 'new')
        self.assertEqual(new['metrics']['precision'], 1)
        self.assertEqual(new['metrics']['tag_accuracy'], 1)
        row = new['assessments'][0]
        self.assertEqual(row['title'], TITLES[0]['title'])
        self.assertEqual(row['tag'], '推理加速')
        self.assertTrue({'title', 'decision', 'topic_relevance', 'information_value',
                         'reason', 'evidence_quote', 'tag'}.issubset(row))


    def test_offline_replay_exports_exact_title_contract(self):
        samples = [json.loads(line) for line in (ROOT / 'tests/fixtures/news_quality_real.jsonl').read_text().splitlines()]
        # Deliberately predict rejection for every row: this is a plumbing test,
        # never presented as measured AI accuracy.
        response = json.dumps([dict(id=s['id'], tag_id=None, topic_relevance=.1,
            information_value=.1, decision='reject', reason='plumbing test only', evidence_quote='') for s in samples])
        deploy = ROOT / '.deploy'
        deploy.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=deploy) as directory:
            output = Path(directory)
            replay = output / 'responses.json'
            replay.write_text(json.dumps({'old': response, 'new': response}))
            # A fixed baseline avoids relying on repository HEAD in CI.
            baseline = output / 'baseline.txt'
            baseline.write_text('[system]\nold\n[user]\n{news_list} {tags_list}')
            with patch('evaluate_filter_live.select_real_samples', return_value=(samples, [])):
                self.assertEqual(main(['--responses', str(replay), '--old-prompt', str(baseline),
                                       '--output-dir', str(output)]), 0)
            rows = json.loads((output / 'assessments-new.json').read_text())
            self.assertEqual([r['title'] for r in rows], [s['title'] for s in samples])
            self.assertTrue(all(r['tag'] is None and r['decision'] == 'reject' for r in rows))
            result = json.loads((output / 'evaluation.json').read_text())
            self.assertEqual(result['evaluations']['new']['metrics']['fn'], 12)
            self.assertFalse(result['evaluations']['new']['human_reviewed'])

    def test_shared_request_and_output_guard(self):
        prompt = '[system]\neditor\n[user]\n{tags_list}\n{news_list}'
        request = requests(TITLES, TAGS, '', prompt)
        self.assertIn(TITLES[0]['title'], request[-1]['content'])
        with patch('sys.stderr'), self.assertRaises(SystemExit):
            main(['--output-dir', str(ROOT / 'output-forbidden')])


if __name__ == '__main__':
    unittest.main()
