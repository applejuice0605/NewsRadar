"""Canonical, source-owned material shared by web, notifications and audio.

No model, network or database writes here. Syndication and progress are conservative
candidates; neither suppresses articles or proves independent corroboration.
"""
from __future__ import annotations
import hashlib
import json
import re
from datetime import date, datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

POLICY_VERSION = 'facts-v1'


def canonical_url(value):
    if not isinstance(value, str) or any(c.isspace() for c in value):
        return ''
    try:
        p = urlsplit(value)
        if p.scheme not in ('https', 'http') or not p.hostname or p.username or p.password:
            return ''
        query = [(k,v) for k,v in parse_qsl(p.query, keep_blank_values=True)
                 if not k.lower().startswith('utm_') and k.lower() not in {'spm','fbclid','gclid'}]
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip('/') or '/', urlencode(sorted(query)), ''))
    except ValueError:
        return ''


def stable_id(*values):
    return hashlib.sha256('\x1f'.join(str(x) for x in values).encode()).hexdigest()[:18]


def normalized_title(title):
    title = re.sub(r'^\s*\[(?:IT之家|转载|转发|快讯)\]\s*', '', title, flags=re.I)
    return re.sub(r'[^\w.%/-]+|_', '', title.casefold())


def entity(title):
    text = title.casefold()
    for name in ('deepseek','vllm','openai','gpt','claude','anthropic','gemini','qwen','kimi','codex','cursor','minimax','mcp','agent'):
        if re.search(r'(?<![a-z])' + name + r'(?![a-z])', text):
            return name
    for name in ('混元','昇腾','华为','腾讯','阿里','字节','谷歌','微软'):
        if name in title:
            return name
    return ''


def tokens(title):
    versions = set(re.findall(r'(?i)(?:\bv\d+(?:\.\d+){0,3}|(?:gpt|claude|gemini|deepseek|qwen|kimi)[-\s]*(?:v)?\d+(?:\.\d+){0,2}|版本\s*\d+(?:\.\d+){0,3})', title))
    metrics = set(re.findall(r'\d+(?:\.\d+)?\s*(?:%|％|倍|毫秒|ms\b|秒|元|美元|token/s|tokens/s|万亿参数|亿参数)', title, flags=re.I))
    return {v.casefold().replace(' ','') for v in versions}, {v.casefold().replace(' ','') for v in metrics}


def annotate_articles(articles):
    """Keep every article; attach exact-copy candidates and progress hints."""
    groups, history, output = {}, {}, []
    seen_titles, seen_versions, seen_metrics = {}, {}, {}
    for source in sorted(articles, key=lambda x: (str(x.get('date','')), str(x.get('first_seen_at') or ''), str(x.get('id','')))):
        row = dict(source)
        row['url'] = canonical_url(row.get('url'))
        row['summary'] = str(row.get('summary') or '')
        row['source'] = str(row.get('source') or row.get('source_name') or '')
        row['date'] = str(row.get('date') or '')
        row['id'] = row.get('id') or 'fact_' + stable_id(row['title'], row.get('source_id'), row['url'])
        key = normalized_title(row['title'])
        url_key = row['url']
        group = groups.get('title:'+key) or (groups.get('url:'+url_key) if url_key else None)
        if not group:
            group = {'id':'copy_'+stable_id(key), 'article_ids':[], 'source_names':[], 'basis': 'exact_title_or_url'}
        group['article_ids'].append(row['id'])
        name = str(row.get('source') or row.get('source_name') or '')
        if name not in group['source_names']:
            group['source_names'].append(name)
        groups['title:'+key] = group
        if url_key:
            groups['url:'+url_key] = group
        row['syndication'] = {'group_id':group['id'], 'basis':'exact_title_or_url_candidate',
                              'independent_verified':False, 'note':'同标题或同URL仅为同稿候选；不同域名也不证明独立报道'}
        subject = entity(row['title'])
        previous = history.get(subject) if subject else None
        kind, label, new_values = 'first_seen', '首次收录，非首次发生', []
        new_v, new_m = tokens(row['title'])
        if previous:
            old_v = seen_versions.get(subject, set())
            old_m = seen_metrics.get(subject, set())
            if key in seen_titles.get(subject, set()):
                kind, label = 'repeat_title', '同标题再次出现'
            elif new_v - old_v:
                kind, label, new_values = 'new_version', '标题出现新版本，待核实', sorted(new_v-old_v)
            elif new_m-old_m:
                kind, label, new_values = 'new_metrics', '标题出现新指标，待核实', sorted(new_m-old_m)
            elif re.search(r'评测|实测|结论|修复|新增|弃用|降价|漏洞|发布', row['title']):
                kind, label = 'new_conclusion_candidate', '标题出现进展线索，待核实'
            else:
                kind, label = 'changed_title', '相关报道，增量待确认'
        row['progress'] = {'kind':kind,'label':label,'new_values':new_values,
                           'previous_article_id': previous['id'] if previous else None,
                           'basis':'已收录标题比较；不作为删除或推送拦截条件'}
        if row.get('appearances',1)>1 and row.get('first_date') != row.get('last_date'):
            row['progress']['repeated_across_days'] = True
        if subject:
            history[subject] = row
            seen_titles.setdefault(subject, set()).add(key)
            seen_versions.setdefault(subject, set()).update(new_v)
            seen_metrics.setdefault(subject, set()).update(new_m)
        output.append(row)
    unique_groups = {g['id']:g for g in groups.values()}
    return output, list(unique_groups.values())


def source_catalog(articles):
    return {a['id']: {'title': a['title'], 'url':a.get('url',''),
                     'source':a.get('source') or a.get('source_name',''),
                     'summary':a.get('summary') or '', 'primary_materials':a.get('primary_materials',[])} for a in articles}


def make_fact_pack(articles, day, claims=None, evidence_sources=None, generated_at=None):
    day = str(day)
    if date.fromisoformat(day).isoformat() != day:
        raise ValueError('Invalid pack date')
    annotated, groups = annotate_articles(articles)
    selected = [{k:v for k,v in a.items() if k != 'report_url'} for a in annotated if str(a['date']) == day]
    original_by_id={str(a.get('id')):a for a in articles}
    for row in selected:
        previous=original_by_id.get(str(row['id']),{}).get('progress')
        if isinstance(previous,dict) and previous.get('basis'):
            row['progress']=previous
    # Callers supply only independently validated model claims. No new AI claims here.
    catalog = evidence_sources if evidence_sources is not None else source_catalog(selected)
    catalog = {i: {'title':str(r.get('title') or ''), 'url':canonical_url(r.get('url')),
                   'source':str(r.get('source') or ''), 'summary':str(r.get('summary') or '')}
               for i,r in catalog.items() if isinstance(r,dict)}
    pack = {'schema_version':1, 'policy_version':POLICY_VERSION, 'date':day,
            'generated_at':generated_at or datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'articles':selected, 'claims':claims or [],
            'evidence_sources':catalog,
            'syndication_groups':[g for g in groups if any(i in {a['id'] for a in selected} for i in g['article_ids'])],
            'independent_source_count':None,
            'limitations':['同稿候选不等于确认转载；来源独立性未知','标题/摘要是来源陈述，不等于独立核实','进展比较不删除新闻']}
    pack['material_hash'] = stable_id(json.dumps({'articles':selected,'claims':pack['claims'],'sources':pack['evidence_sources']},ensure_ascii=False,sort_keys=True,default=str))
    return pack


def json_for_html(pack):
    return json.dumps(pack,ensure_ascii=False,default=str).replace('&','\\u0026').replace('<','\\u003c').replace('>','\\u003e')
