"""Publish factual-edition provenance and a cutoff label, without model calls."""
import argparse
import html
import json
from pathlib import Path
import re


def publish(repo,site):
    root=repo/'TrendRadar/output/meta/daily_editions'
    for path in sorted(root.glob('*/edition.json')):
        from trendradar.ai.daily_edition import load_edition
        ed=load_edition(path);day=ed['date']
        public={k:ed[k] for k in ['date','edition_id','news_cutoff_at','frozen_at','facts_sha256']}
        public['selection']=ed.get('selection',{})
        public['sources']=[{**source,'facts':topic['facts']} for source,topic in zip(ed['topic_sources'],ed['writer_plan']['topics'])]
        target=site/'editions'/f'{day}.json';target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(json.dumps(public,ensure_ascii=False,indent=2)+'\n')
        candidates=list((site/'reports'/day).glob('*.html'))
        if (site/'index.html').exists():candidates.append(site/'index.html')
        for page in candidates:
            text=page.read_text()
            # The embedded fact pack proves this page actually used the edition.
            if ed['edition_id'] not in text or 'daily-edition-note' in text:continue
            label=(f'<aside id="daily-edition-note" style="padding:12px 20px">'
                f'晚间分析新闻截至 {html.escape(day)} 22:00（北京时间）；播客共用此事实版本。'
                f'<a href="/editions/{day}.json">查看本期来源</a>。看板后续采集继续更新。</aside>')
            text=re.sub(r'(<body\b[^>]*>)',lambda m:m[0]+label,text,count=1)
            page.write_text(text)


if __name__=='__main__':
    import sys
    parser=argparse.ArgumentParser();parser.add_argument('--repo',type=Path,required=True);parser.add_argument('--site',type=Path,required=True)
    args=parser.parse_args();sys.path.insert(0,str(args.repo/'TrendRadar'))
    publish(args.repo,args.site)
