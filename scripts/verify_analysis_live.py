#!/usr/bin/env python3
"""One explicit live-model smoke test against read-only collected news. No sending."""
import argparse
import contextlib
from dataclasses import asdict
from datetime import datetime
import html
import io
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'TrendRadar'))
from trendradar.ai.analyzer import AIAnalyzer
from trendradar.ai.formatter import render_ai_analysis_html_rich, render_ai_analysis_feishu
from trendradar.ai.fact_pack import make_fact_pack
from trendradar.core.loader import load_config
from generate_site_insights import load_articles, peripheral_reason


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-repo', type=Path, default=Path('/Users/bruce/Coding/NewsRadar-production'))
    parser.add_argument('--output-dir', type=Path, default=ROOT / '.deploy/first-batch')
    parser.add_argument('--fact-pack', type=Path, help='Use canonical editorial materials instead of reselecting titles')
    args = parser.parse_args()
    articles, skipped = load_articles(args.data_repo / 'TrendRadar/output')
    today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
    eligible = [a for a in articles if a['date'] <= today and a.get('url') and a.get('tags')
                and max(float(t[1]) for t in a['tags']) >= .7 and not peripheral_reason(a['title'])]
    if not eligible:
        raise SystemExit('No matched news available for live verification')
    day = max(a['date'] for a in eligible)
    chosen, seen = [], set()
    for article in sorted(eligible, key=lambda a: max(float(t[1]) for t in a['tags']), reverse=True):
        if article['date'] != day or article['url'] in seen:
            continue
        seen.add(article['url'])
        chosen.append({**article, 'source_name': article['source']})
        if len(chosen) == 12:
            break
    input_pack = None
    if args.fact_pack:
        input_pack = json.loads(args.fact_pack.read_text())
        if input_pack.get('schema_version') != 1 or not input_pack.get('articles'):
            raise SystemExit('Canonical pack is empty or unsupported')
        day = datetime.strptime(input_pack['date'], '%Y-%m-%d').date()
        chosen = [{**a, 'source_name': a.get('source', '')} for a in input_pack['articles'][:12]]
    # Avoid leaking masked key prefixes/config diagnostics to saved evidence.
    with contextlib.redirect_stdout(io.StringIO()):
        config = load_config(str(ROOT / 'TrendRadar/config/config.yaml'))
        analyzer = AIAnalyzer(config['AI'], {**config['AI_ANALYSIS'], 'MAX_NEWS_FOR_ANALYSIS': 12},
                              lambda: datetime.now(ZoneInfo('Asia/Shanghai')))
        result = analyzer.analyze([{'word': 'AI、测试与工程实践', 'titles': chosen}])
    if result.success and input_pack:
        result.fact_pack = make_fact_pack(chosen, day.isoformat(), result.evidence_claims, result.evidence_sources)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    payload = {'verified_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
               'source_date': day.isoformat(), 'input_count': len(chosen), 'skipped_databases': len(skipped),
               'result': asdict(result), 'limitation': '验证真实模型结构与引用；不证明来源真实性或AI判断正确性。'}
    (args.output_dir / 'live-analysis.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    if not result.success:
        print(json.dumps({'success': False, 'error': 'Live model did not return grounded output; see private artifact'}, ensure_ascii=False))
        return 1
    (args.output_dir / 'fact-pack.json').write_text(json.dumps(result.fact_pack, ensure_ascii=False, indent=2, default=str))
    (args.output_dir / 'feishu-preview.md').write_text(render_ai_analysis_feishu(result))
    title = 'AI 分析内测验证 · ' + day.isoformat()
    generated = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')
    page = ('<!doctype html><html lang="zh-CN"><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>' + title + '</title><style>body{margin:24px auto;padding:0 20px;max-width:900px;font:700 16px/1.7 system-ui;background:#fffef0;color:#1a1a1a}'
            '.ai-block{margin:20px 0;padding:20px;border:4px solid #1a1a1a;box-shadow:4px 4px 0 #1a1a1a;background:white}'
            '.ai-block-title{font-weight:900}a{color:#1943b8;overflow-wrap:anywhere}</style>'
            '<h1>' + html.escape(title) + '</h1><p>生成：' + generated + '（北京时间） · ' + str(len(chosen)) + ' 条真实已采集新闻。此页为内测验证样本，非完整日报。</p>'
            '<p>事实摘录为来源陈述；链接可追溯不代表已经独立核实。AI 判断单独标注。</p>'
            + render_ai_analysis_html_rich(result) + '</html>')
    (args.output_dir / 'analysis-preview.html').write_text(page)
    print(json.dumps({'success': True, 'input_count': len(chosen), 'accepted_claims': len(result.evidence_claims),
                      'rejected_claims': result.evidence_rejected_count, 'source_date': day.isoformat()}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
