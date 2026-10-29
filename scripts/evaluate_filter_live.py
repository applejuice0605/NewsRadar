#!/usr/bin/env python3
"""Offline-first paired prompt evaluation against explicit editorial annotations.

Default: read real DBs through generate_site_insights.load_articles (mode=ro),
verify the manually selected balanced fixture, export identical requests. No .env
loading, DB writes, AI imports or network calls. --live explicitly enables model
calls using AI_API_KEY from the process environment; it never loads a .env file.
Alternatively --responses replays {"old": "raw response", "new": "raw response"}.
All artifacts are confined to .deploy. Classifier scores are never truth labels.

Examples:
  python scripts/evaluate_filter_live.py
  python scripts/evaluate_filter_live.py --responses .deploy/quality/responses.json
  python scripts/evaluate_filter_live.py --live --model provider/model
"""
import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from generate_site_insights import load_articles
from evaluate_news_quality import evaluate

# Import pure parser without trendradar.ai.__init__ (which imports AI clients).
_spec = importlib.util.spec_from_file_location('editorial_quality_eval', ROOT / 'TrendRadar/trendradar/ai/editorial_quality.py')
quality = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(quality)

TAG_NAMES = [
    'AI 趋势与大模型产品进展', 'Agent、计算机控制与 OpenClaw/Claw 生态',
    'MCP 与 Agent 基础设施', 'AI 测试、评估与质量保障', 'AI 编程工具与开发效率',
    '可观测性、LLMOps 与 AI 工程化', 'AI 基础设施、推理加速与云算力',
    '大厂战略、机器人与跨行业影响',
]


def select_real_samples(data_dir, fixture):
    """Match annotated keys AND exact titles; never derive labels from DB tags."""
    samples = [json.loads(line) for line in fixture.read_text(encoding='utf-8').splitlines() if line.strip()]
    if len(samples) < 24 or len({s['id'] for s in samples}) != len(samples):
        raise ValueError('Need at least 24 uniquely labelled real samples')
    if any(type(s.get('expected_accept')) is not bool or s.get('annotation_kind') != 'editorial_manual'
           or not s.get('editorial_reason') for s in samples):
        raise ValueError('Explicit independent editorial annotations required')
    accepted = sum(s['expected_accept'] for s in samples)
    if accepted != len(samples) - accepted:
        raise ValueError('Fixture must be balanced between accept and reject')
    articles, skipped = load_articles(data_dir)
    real = {a['key']: a for a in articles}
    for sample in samples:
        article = real.get(sample['real_article_key'])
        if article is None or article['title'] != sample['title']:
            raise ValueError('Real DB title missing or changed for sample ' + str(sample['id']))
        if type(sample['id']) is not int:
            raise ValueError('Sample IDs must be integer classifier IDs')
        tid = sample.get('expected_tag_id')
        if sample['expected_accept'] and (type(tid) is not int or not 1 <= tid <= len(TAG_NAMES)
                or sample.get('expected_tag') != TAG_NAMES[tid - 1]):
            raise ValueError('Positive samples require a tag from the fixed evaluation taxonomy')
        quote = sample.get('evidence_quote', '')
        if (sample['expected_accept'] and not quote) or (quote and quote not in sample['title']):
            raise ValueError('Editorial evidence must quote the exact title')
    return samples, skipped


def prompt_sections(text):
    if '[system]' not in text or '[user]' not in text:
        raise ValueError('Prompt needs [system] and [user] sections')
    system, user = text.split('[user]', 1)
    return system.split('[system]', 1)[1].strip(), user.strip()


def requests(samples, tags, interests, prompt, min_topic=.7, min_value=.5):
    system, user = prompt_sections(prompt)
    values = {'interests_content': interests,
              'tags_list': '\n'.join(f"{t['id']}. {t['tag']}: {t.get('description', '')}" for t in tags),
              'news_count': str(len(samples)),
              'news_list': '\n'.join(f"{s['id']}. [{s.get('source', '')}] {s['title']}" for s in samples),
              'min_topic_relevance': str(min_topic), 'min_information_value': str(min_value)}
    for key, value in values.items():
        system = system.replace('{' + key + '}', value)
        user = user.replace('{' + key + '}', value)
    return [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]


def parse_response(response):
    if not isinstance(response, str):
        return None
    text = response.strip()
    if text.startswith('```') and text.endswith('```'):
        text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
    try:
        result = json.loads(text)
        return result if isinstance(result, list) else None
    except (ValueError, IndexError):
        return None


def evaluate_response(samples, tags, response, variant, config=None):
    data = parse_response(response)
    assessments = quality.parse_decisions(data, samples, tags, config)
    # Historical prompt explicitly omitted all nonmatches. Only for this offline
    # selection comparison, interpret omissions as rejects; this cannot distinguish
    # truncation from intentional omission. Production never marks these analyzed.
    if variant == 'old' and data is not None:
        for index, a in enumerate(assessments):
            if a['reason_code'] == 'missing_decision':
                a = quality.legacy_assessment(a['id'], decision='reject')
                a['title'] = samples[index]['title']
                a['tag'] = None
                a['reason'] = 'legacy_sparse_omission: inferred reject for offline comparison only'
                assessments[index] = a
    complete = {a['id']: a for a in assessments if a['completed']}
    names = {t['id']: t['tag'] for t in tags}
    predictions = [dict(id=i, accepted=a['decision'] == 'accept', tag=names.get(a['tag_id'])) for i, a in complete.items()]
    metric = evaluate([s for s in samples if s['id'] in complete], predictions)['ai_evaluation']
    failures = [a for a in assessments if not a['completed']]
    return dict(metrics=metric, completed_count=len(complete), input_count=len(samples),
                failure_count=len(failures), coverage=len(complete) / len(samples),
                failed_ids=[a['id'] for a in failures], assessments=assessments,
                metric_scope='completed decisions only; request failures reported separately',
                legacy_omission_caveat=variant == 'old',
                truth_source='explicit editorial_manual fixture; no classifier scores used as truth',
                human_reviewed=all(s.get('human_reviewed', False) for s in samples))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'TrendRadar/output')
    parser.add_argument('--samples', type=Path, default=ROOT / 'tests/fixtures/news_quality_real.jsonl')
    parser.add_argument('--output-dir', type=Path, default=ROOT / '.deploy/filter-quality')
    parser.add_argument('--old-prompt', type=Path, help='Default: committed HEAD prompt (captured before local edits)')
    parser.add_argument('--new-prompt', type=Path, default=ROOT / 'TrendRadar/config/ai_filter/prompt.txt')
    parser.add_argument('--tags', type=Path, help='Optional shared JSON tag list; must match fixture taxonomy')
    parser.add_argument('--ai-config', type=Path, help='Local JSON AI config or object containing AI; never reads .env')
    parser.add_argument('--model', help='Override config MODEL for both prompts')
    parser.add_argument('--max-tokens', type=int, default=8000)
    parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('--api-base', help='Override config API_BASE')
    parser.add_argument('--min-topic-relevance', type=float, default=.7)
    parser.add_argument('--min-information-value', type=float, default=.5)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--live', action='store_true')
    mode.add_argument('--responses', type=Path)
    args = parser.parse_args(argv)
    if args.max_tokens < 1 or args.timeout <= 0 or not math.isfinite(args.timeout):
        parser.error('--max-tokens and --timeout must be positive')
    ai_config = {}
    if args.ai_config:
        supplied = json.loads(args.ai_config.read_text(encoding='utf-8'))
        if not isinstance(supplied, dict):
            parser.error('--ai-config must contain a JSON object')
        ai_config = supplied.get('AI', supplied)
        if not isinstance(ai_config, dict):
            parser.error('AI config must be a JSON object')
        ai_config = dict(ai_config)
    model = args.model or ai_config.get('MODEL', 'deepseek/deepseek-chat')
    # Same model/settings for both variants; no fallback models during evaluation.
    ai_config.update(MODEL=model, TEMPERATURE=0, MAX_TOKENS=args.max_tokens,
                     TIMEOUT=args.timeout, NUM_RETRIES=0, FALLBACK_MODELS=[])
    if args.api_base is not None:
        ai_config['API_BASE'] = args.api_base
    out = args.output_dir.resolve()
    if not out.is_relative_to((ROOT / '.deploy').resolve()):
        parser.error('All evaluation outputs must be under .deploy')
    samples, skipped = select_real_samples(args.data_dir, args.samples)
    tags = json.loads(args.tags.read_text()) if args.tags else [dict(id=i, tag=n) for i, n in enumerate(TAG_NAMES, 1)]
    if not isinstance(tags, list) or any(type(t.get('id')) is not int for t in tags) or {
            t['id']: t.get('tag') for t in tags} != dict(enumerate(TAG_NAMES, 1)) or len(tags) != len(TAG_NAMES):
        raise ValueError('Shared tags must match fixture IDs/names; descriptions may be customized')
    config = dict(MIN_TOPIC_RELEVANCE=args.min_topic_relevance, MIN_INFORMATION_VALUE=args.min_information_value)
    quality.thresholds(config)
    old = args.old_prompt.read_text(encoding='utf-8') if args.old_prompt else subprocess.run(
        ['git', 'show', 'HEAD:TrendRadar/config/ai_filter/prompt.txt'], cwd=ROOT,
        check=True, capture_output=True, text=True).stdout
    prompts = {'old': old, 'new': args.new_prompt.read_text(encoding='utf-8')}
    interests = (ROOT / 'TrendRadar/config/ai_interests.txt').read_text(encoding='utf-8')
    paired = {variant: requests(samples, tags, interests, prompt, args.min_topic_relevance, args.min_information_value)
              for variant, prompt in prompts.items()}
    manifest = dict(schema_version=1, mode='live' if args.live else 'replay' if args.responses else 'prepare',
                    model=model, temperature=0, max_tokens=args.max_tokens, timeout=args.timeout, thresholds=config, tags=tags,
                    samples=samples, skipped_databases=skipped,
                    prompt_sha256={k: hashlib.sha256(v.encode()).hexdigest() for k,v in prompts.items()},
                    requests=paired, scope='balanced editorial regression; not representative traffic accuracy',
                    annotation_caveat='Codex editorial annotations; not human-adjudicated; underlying headlines not fact-checked')
    out.mkdir(parents=True, exist_ok=True)
    (out / 'requests.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    for variant, prompt in prompts.items():
        (out / (variant + '-prompt.txt')).write_text(prompt, encoding='utf-8')
    responses = None
    if args.live:
        # Prevent litellm import-time cost-map fetching. Only chat below is live.
        os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
        sys.path.insert(0, str(ROOT / 'TrendRadar'))
        from trendradar.ai.client import AIClient
        client = AIClient(ai_config)
        responses = {}
        for variant in ('old', 'new'):
            try:
                responses[variant] = client.chat(paired[variant])
            except Exception as exc:
                responses[variant] = None
                manifest.setdefault('request_errors', {})[variant] = type(exc).__name__
        (out / 'responses.json').write_text(json.dumps(responses, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    elif args.responses:
        responses = json.loads(args.responses.read_text(encoding='utf-8'))
    report = dict(mode=manifest['mode'], sample_count=len(samples), model=model,
                  request_errors=manifest.get('request_errors', {}),
                  annotation_caveat=manifest['annotation_caveat'], evaluations=None)
    if responses is not None:
        if not isinstance(responses, dict) or not {'old', 'new'}.issubset(responses):
            raise ValueError('Replay must contain both old and new raw responses')
        report['evaluations'] = {v: evaluate_response(samples, tags, responses[v], v, config) for v in ('old', 'new')}
        # Integration contract: exact-title rows, predicted tag NAME. The expected
        # fixture tag is used only by metrics, never copied into predictions.
        report['assessment_files'] = {}
        for variant, evaluation in report['evaluations'].items():
            assessment_path = out / ('assessments-' + variant + '.json')
            assessment_path.write_text(json.dumps(evaluation['assessments'], ensure_ascii=False,
                                                 indent=2, allow_nan=False) + '\n', encoding='utf-8')
            report['assessment_files'][variant] = str(assessment_path)
    (out / 'evaluation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k != 'evaluations'}, ensure_ascii=False))
    return 1 if report['evaluations'] and any(r['failure_count'] for r in report['evaluations'].values()) else 0


if __name__ == '__main__':
    raise SystemExit(main())
