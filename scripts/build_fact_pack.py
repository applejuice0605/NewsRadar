#!/usr/bin/env python3
"""Build bounded editorial packs from read-only collection; output only to site/cache."""
from __future__ import annotations
import argparse
import importlib.util
import json
from pathlib import Path

from generate_product_experience import build_experience

ROOT=Path(__file__).resolve().parents[1]

def module(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'TrendRadar/trendradar/ai'/f'{name}.py')
    loaded=importlib.util.module_from_spec(spec);spec.loader.exec_module(loaded)
    return loaded

facts=module('fact_pack')
primary=module('primary_materials')


def build(repo, site, *, enrich=False, max_primary=5, cache=None, assessments=None):
    experience=build_experience(repo)
    today=experience['as_of']
    articles=experience['articles']
    editorial={}
    if assessments and Path(assessments).is_file():
        values=json.loads(Path(assessments).read_text())
        rows = values.get('items', values.get('assessments', [])) if isinstance(values, dict) else values
        if isinstance(values, dict) and 'runs' in values:
            rows = [item for run in values['runs'] for item in run.get('assessments', []) if item.get('completed')]
        for row in rows:
            if isinstance(row,dict):
                editorial[facts.normalized_title(str(row.get('title','')))]=row
    if not assessments:
        path = repo/'TrendRadar/output/meta/filter_quality'/f'{today}.json'
        if path.is_file():
            values=json.loads(path.read_text())
            for run in values.get('runs',[]):
                for row in run.get('assessments',[]):
                    if row.get('completed'):
                        editorial[facts.normalized_title(str(row.get('title','')))]=row
    prepared=[]
    for source in articles:
        row=dict(source)
        assessment=editorial.get(facts.normalized_title(row['title']))
        if assessment:
            row['assessment']=assessment
        else:
            row['assessment']={'decision':'legacy_unknown','information_value':None,
                'topic_relevance':max((t['relevance'] for t in row['tags']),default=0),
                'reason':'旧分类仅有相关度，未冒充新信息价值评分'}
        prepared.append(row)
    annotated,groups=facts.annotate_articles(prepared)
    ids=experience['recommendations']['global_important']
    by_id={r['id']:r for r in annotated}
    selected=[by_id[i] for i in ids if i in by_id and by_id[i]['date']==today]
    # A real new-policy rejection must not remain a lead just because it ranked high.
    selected=[r for r in selected if r['assessment'].get('decision') not in ('reject','rejected')]
    if enrich:
        selected=primary.PrimaryEnricher(cache or ROOT/'.deploy/primary-cache',max_articles=max_primary).enrich(selected)
    pack=facts.make_fact_pack(selected,today)
    pack['screening_decisions']=[r['assessment'] for r in prepared if r['date']==today and r['assessment'].get('completed')]
    pack['material_hash']=facts.stable_id(json.dumps({k:v for k,v in pack.items() if k!='material_hash'},ensure_ascii=False,sort_keys=True,default=str))
    site.mkdir(parents=True,exist_ok=True)
    (site/'facts').mkdir(exist_ok=True)
    (site/'facts'/f'{today}.json').write_text(json.dumps(pack,ensure_ascii=False,indent=2,default=str)+'\n')
    (site/'facts'/'latest.json').write_text(json.dumps(pack,ensure_ascii=False,indent=2,default=str)+'\n')
    return pack


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--repo-dir',type=Path,default=ROOT)
    parser.add_argument('--site-dir',type=Path,default=ROOT/'.deploy/preview')
    parser.add_argument('--enrich',action='store_true')
    parser.add_argument('--max-primary',type=int,default=5)
    parser.add_argument('--assessments',type=Path)
    args=parser.parse_args()
    result=build(args.repo_dir,args.site_dir,enrich=args.enrich,max_primary=args.max_primary,assessments=args.assessments)
    print(json.dumps({'date':result['date'],'articles':len(result['articles']),
        'primary_material_count':sum(len(a.get('primary_materials',[])) for a in result['articles'])},ensure_ascii=False))


if __name__=='__main__': main()
