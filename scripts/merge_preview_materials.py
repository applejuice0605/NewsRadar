#!/usr/bin/env python3
"""Overlay explicitly validated preview material without freezing fresh collection."""
import argparse
import importlib.util
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('merge_facts',ROOT/'TrendRadar/trendradar/ai/fact_pack.py')
facts=importlib.util.module_from_spec(spec);spec.loader.exec_module(facts)


def merge(current, material):
    if current['date']!=material['date']: return current
    source={facts.canonical_url(a.get('url')):a for a in material.get('articles',[])}
    screened={facts.normalized_title(a.get('title','')):a for a in material.get('screening_decisions',[]) if isinstance(a,dict) and a.get('completed')}
    current['screening_decisions']=list(screened.values())
    added_ids=set()
    for article in current['articles']:
        decision=screened.get(facts.normalized_title(article['title']))
        if decision: article['assessment']=decision
        old=source.get(facts.canonical_url(article.get('url')))
        if not old or facts.normalized_title(old['title'])!=facts.normalized_title(article['title']): continue
        added_ids.add(old['id'])
        for key in ('assessment','primary_materials','primary_status'):
            if key in old: article[key]=old[key]
    # Only claims linked to matching, current source content can accompany the brief.
    sources=material.get('evidence_sources',{})
    current_urls={facts.canonical_url(a['url']):a for a in current['articles'] if a.get('assessment',{}).get('decision') not in ('reject','rejected')}
    claims=[]
    used=set()
    for claim in material.get('claims',[]):
        cited=[sources.get(i,{}) for i in claim.get('source_ids',[])]
        if not cited: continue
        if not any(facts.canonical_url(s.get('url')) in current_urls and
                   facts.normalized_title(s.get('title',''))==facts.normalized_title(current_urls[facts.canonical_url(s['url'])]['title']) for s in cited): continue
        claims.append(claim);used.update(claim['source_ids'])
    current['claims']=claims
    current['evidence_sources']={i:sources[i] for i in used if i in sources}
    if not claims: current['evidence_sources']=facts.source_catalog(current['articles'])
    current['analysis_generated_at']=material.get('generated_at')
    current['material_hash']=facts.stable_id(json.dumps({k:v for k,v in current.items() if k!='material_hash'},ensure_ascii=False,sort_keys=True,default=str))
    return current


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--site-dir',type=Path,required=True);parser.add_argument('--material',type=Path,required=True);args=parser.parse_args()
    path=args.site_dir/'facts/latest.json'
    if not path.is_file(): return
    current=json.loads(path.read_text());material=json.loads(args.material.read_text())
    result=merge(current,material)
    for target in (path,args.site_dir/'facts'/f"{result['date']}.json"):
        target.write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str)+'\n')


if __name__=='__main__': main()
