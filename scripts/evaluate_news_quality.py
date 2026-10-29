#!/usr/bin/env python3
"""Reproducible editorial evaluation: separate rule safety from AI relevance quality."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'TrendRadar'))
from generate_site_insights import peripheral_reason


def evaluate(samples, predictions=None):
    failures = []
    for row in samples:
        excluded = peripheral_reason(row['title']) is not None
        if excluded != row['expected_peripheral']:
            failures.append(row['id'])
    result = {'sample_count': len(samples), 'rule_failures': failures,
              'rule_pass_count': len(samples) - len(failures),
              'scope': '编辑判断样本；规则回归不等于AI筛选准确率；不代表真实流量分布',
              'annotation_authors': sorted({row.get('annotation_author', 'unspecified') for row in samples}),
              'human_reviewed': all(row.get('human_reviewed', False) for row in samples),
              'ai_evaluation': None}
    if predictions is not None:
        truth = {row['id']: row for row in samples}
        ids = [row['id'] for row in predictions]
        if len(ids) != len(set(ids)) or set(ids) != set(truth):
            raise ValueError('Predictions must cover every sample ID exactly once')
        tp = fp = fn = tn = classified = positive = 0
        for prediction in predictions:
            actual = truth[prediction['id']]
            if not isinstance(prediction.get('accepted'), bool):
                raise ValueError('accepted must be boolean')
            a, p = actual['expected_accept'], prediction['accepted']
            tp += a and p
            fp += not a and p
            fn += a and not p
            tn += not a and not p
            if a:
                positive += 1
                classified += p and prediction.get('tag') == actual['expected_tag']
        result['ai_evaluation'] = {'tp': tp, 'fp': fp, 'fn': fn, 'tn': tn,
            'precision': tp / (tp + fp) if tp + fp else None,
            'recall': tp / (tp + fn) if tp + fn else None,
            'positive_classification_accuracy': classified / positive if positive else None,
            'tag_accuracy': classified / tp if tp else None,
            'false_positive_ids': [p['id'] for p in predictions if p['accepted'] and not truth[p['id']]['expected_accept']],
            'false_negative_ids': [p['id'] for p in predictions if not p['accepted'] and truth[p['id']]['expected_accept']]}
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--samples', type=Path, default=ROOT / 'tests/fixtures/news_quality_editorial.jsonl')
    parser.add_argument('--predictions', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    samples = [json.loads(line) for line in args.samples.read_text().splitlines() if line.strip()]
    predictions = json.loads(args.predictions.read_text()) if args.predictions else None
    result = evaluate(samples, predictions)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        if not args.output.resolve().is_relative_to((ROOT / ".deploy").resolve()):
            parser.error("--output must be under .deploy")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + '\n')
    print(text)
    return bool(result['rule_failures'])


if __name__ == '__main__':
    raise SystemExit(main())
