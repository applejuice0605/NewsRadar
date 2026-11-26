"""One immutable, cutoff-bound factual edition shared by analysis and audio.

Collection keeps running. Selection, fetched bodies and the projected facts
freeze once; retries never rebuild an edition from changing databases.
No model call is made while freezing. Hashes identify inputs, not truth.
"""
from __future__ import annotations

from contextlib import contextmanager, closing
from datetime import date, datetime
import fcntl
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False, encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2);f.write('\n');tmp=Path(f.name)
    tmp.replace(path)


@contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:yield
        finally:fcntl.flock(f, fcntl.LOCK_UN)


def _minute(value):
    if not isinstance(value, str):return None
    m=re.search(r'(?:T|\s|^)(\d{2})[:-](\d{2})(?::(\d{2}))?',value)
    if not m:return None
    h,n,s=map(int,(m[1],m[2],m[3] or '0'))
    return h*3600+n*60+s if h<24 and n<60 and s<60 else None


def _day_seconds(value,day):
    if not isinstance(value,str):return None
    if re.match(r"^\d{4}-\d{2}-\d{2}[T ]",value):
        try:
            stamp=datetime.fromisoformat(value.replace('Z','+00:00'))
            stamp=stamp.replace(tzinfo=TZ) if stamp.tzinfo is None else stamp.astimezone(TZ)
            if stamp.date().isoformat()!=day:return None
            return stamp.hour*3600+stamp.minute*60+stamp.second
        except ValueError:return None
    return _minute(value)


def cutoff_candidates(repo, day, articles):
    """Use stored first-crawl and classification times, not lossy UI times.

    Aggregated ranks can include later observations after a delayed launch;
    restore only ranks observed by22:00. Missing temporal evidence fails closed.
    """
    output=Path(repo)/'TrendRadar/output';eligible={};ranks={}
    for kind,table,source in [('news','news_items','hotlist'),('rss','rss_items','rss')]:
        p=output/kind/f'{day}.db'
        if not p.exists():continue
        with closing(sqlite3.connect(f'file:{p}?mode=ro',uri=True)) as c:
            for ident,first in c.execute(f'SELECT id,first_crawl_time FROM {table}'):
                seconds=_day_seconds(first,day)
                if seconds is not None and seconds<=22*3600:eligible[(source,int(ident))]=True
            if kind=='news':
                for ident,rank,when in c.execute('SELECT news_item_id,rank,crawl_time FROM rank_history'):
                    if _day_seconds(when,day) is not None and _day_seconds(when,day)<=22*3600:
                        ranks.setdefault(int(ident),[]).append(int(rank))
    # Only classifications completed by the cutoff are eligible too.
    p=output/'news'/f'{day}.db';qualified={}
    if p.exists():
        with closing(sqlite3.connect(f'file:{p}?mode=ro',uri=True)) as c:
            for ident,source,tag,score,when in c.execute('SELECT r.news_item_id,r.source_type,t.tag,r.relevance_score,r.created_at FROM ai_filter_results r JOIN ai_filter_tags t ON t.id=r.tag_id WHERE r.status="active"'):
                seconds=_day_seconds(when,day)
                if seconds is not None and seconds<=22*3600:
                    qualified.setdefault((source,int(ident)),[]).append((tag,float(score)))
    selected=[]
    for article in articles:
        day_value=article.get('date');day_value=day_value.isoformat() if isinstance(day_value,date) else day_value
        key=(article.get('source_type'),article.get('item_id'))
        if day_value!=day or key not in eligible or key not in qualified:continue
        a=dict(article);a['tags']=qualified[key]
        if key[0]=='hotlist':
            history=[r for r in ranks.get(int(key[1]),[]) if r>0]
            if not history:continue
            a['best_rank']=min(history);a['rank']=min(history)
            a['momentum']=0;a['crawl_count']=len(history)
        selected.append(a)
    return selected


def ranked_candidates(selected,day):
    from podcast_materials import _is_candidate,_selection_key,_normalize_url,_normalize_title
    result=[];urls=set();titles=set()
    for a in sorted(selected,key=_selection_key):
        if not _is_candidate(a,day)[0]:continue
        url=_normalize_url(a.get('url'));title=_normalize_title(a.get('title'))
        if url in urls or title in titles:continue
        urls.add(url);titles.add(title);result.append(a)
    return result


def load_edition(path):
    edition=json.loads(Path(path).read_text())
    identity=edition.get('edition_id')
    payload={k:v for k,v in edition.items() if k not in {'edition_id','edition_path'}}
    if digest(payload)!=identity:raise ValueError('shared edition integrity mismatch')
    return edition


def freeze(repo, now=None, *, settings=None, articles=None, builder=None, planner=None, report_date=None, recover_missing_sources=False, recover_selection=False):
    now=now or datetime.now(TZ)
    if now.tzinfo is None:raise ValueError('timezone-aware time required')
    now=now.astimezone(TZ);target_day=report_date or now.date();day=target_day.isoformat()
    if target_day>now.date():raise ValueError('future edition date rejected')
    if target_day==now.date() and (now.hour,now.minute)<(22,0):raise ValueError('shared edition waits for22:00 Shanghai')
    repo=Path(repo);directory=repo/'TrendRadar/output/meta/daily_editions'/day
    with locked(directory/'.lock'):
        path=directory/'edition.json'
        if path.exists():return load_edition(path)
        failed=directory/'freeze-failed.json'
        if failed.exists():
            previous_failure=json.loads(failed.read_text())
            recoverable=previous_failure.get('message','').startswith('cutoff-selected source body missing before22:00')
            attempted=any((directory/name).exists() for name in ('analysis-attempt.json','analysis-response.json','analysis-result.json'))
            selection_failure = previous_failure.get("message", "").startswith("cutoff source materials insufficient or invalid:")
            can_reselect = (recover_selection and selection_failure and builder is None
                            and (directory/"candidates.json").is_file()
                            and (directory/"source-capture.json").is_file())
            if attempted or not ((recover_missing_sources and recoverable) or can_reselect):
                raise ValueError('frozen selection rejected; no automatic reselection; inspect before explicit recovery')
            # Retain the failed cutoff snapshot and reason; no candidate reload.
            write_json(directory/'recovered-freeze-failure.json',previous_failure)
            failed.rename(directory/'freeze-failed-before-recovery.json')
        script_dir=repo/'scripts'
        if str(script_dir) not in sys.path:sys.path.insert(0,str(script_dir))
        from podcast_materials import build_materials,validate_payload
        from podcast_editorial_plan import build_editorial_plan
        from podcast_dialogue import project_editorial_plan
        candidate_path=directory/'candidates.json'
        if candidate_path.exists():
            candidate_record=json.loads(candidate_path.read_text())
            selected=candidate_record['candidates']
            if digest(selected)!=candidate_record['sha256']:raise ValueError('candidate snapshot hash mismatch')
            for a in selected:a['date']=date.fromisoformat(a['date'])
        else:
            if articles is None:
                from generate_site_insights import load_articles
                articles,_=load_articles(repo/'TrendRadar/output')
            selected=cutoff_candidates(repo,day,articles)
            serialized=[{**a,'date':day} for a in selected]
            write_json(candidate_path,{'candidates':serialized,'sha256':digest(serialized)})
        settings=settings or {}
        try:
            if builder is None:
                capture_path=directory/'source-capture.json'
                capture=json.loads(capture_path.read_text())
                captured=datetime.fromisoformat(capture['captured_at'])
                if captured.astimezone(TZ).date()!=target_day or captured.astimezone(TZ).hour>=22:
                    raise ValueError('source bodies were not captured before cutoff')
                if digest(capture['materials'])!=capture['materials_sha256']:raise ValueError('source capture integrity mismatch')
                ordered=ranked_candidates(selected,target_day)
                by_url={i['url']:i for i in capture['materials']['items']}
                available=[a for a in ordered if a.get('url') in by_url]
                excluded=[{'title':a.get('title'),'url':a.get('url'),'reason':'no_qualified_body_captured_before_cutoff'} for a in ordered if a.get('url') not in by_url]
                limit = min(6, int(settings.get('max_items', 6)))
                authoritative = available[:limit]
                minimum_count = int(settings.get('min_material_items', 3))
                minimum_chars = int(settings.get('min_material_chars', 3000))

                def sufficient(candidates):
                    reports = [by_url[item['url']] for item in candidates
                               if by_url[item['url']].get('source_genre') != 'community_post']
                    return (len(reports) >= minimum_count
                            and sum(len(item['text']) for item in reports) >= minimum_chars)

                replacement = None
                if not sufficient(authoritative):
                    # Preserve as much of the highest-ranked prefix as possible.
                    for index in reversed(range(len(authoritative))):
                        for rank, candidate in enumerate(available[limit:], start=limit + 1):
                            trial = authoritative[:index] + authoritative[index + 1:] + [candidate]
                            if sufficient(trial):
                                replacement = {'removed_rank': index + 1, 'added_rank': rank}
                                authoritative = trial
                                break
                        if replacement:
                            break
                materials={**capture['materials'],'items':[by_url[a['url']] for a in authoritative]}
                selection={'source_capture_at':capture['captured_at'],'no_post_cutoff_fetch':True,
                    'policy':'rank_among_pre_cutoff_body_qualified_candidates_v3',
                    'candidate_count':len(ordered),'body_qualified_count':len(available),
                    'selected_count':len(authoritative),'excluded_candidates':excluded}
                if replacement:
                    selection['replacement'] = replacement

            else:
                materials,selection=builder(repo,target_day,directory/'source-cache',candidates=selected,
                max_items=int(settings.get('max_items',6)),max_fetch=int(settings.get('max_fetch',20)),
                max_age_days=int(settings.get('max_age_days',3)),budget_seconds=float(settings.get('budget_seconds',60)))
            write_json(directory/'selection-review.json',selection)
            valid,errors=validate_payload(materials,target_day)
            core=[i for i in materials['items'] if i.get('source_genre')!='community_post']
            if not valid or len(core)<int(settings.get('min_material_items',3)) or sum(len(i['text']) for i in core)<int(settings.get('min_material_chars',3000)):
                raise ValueError(f'cutoff source materials insufficient or invalid: reports={len(core)}, '
                                 f'characters={sum(len(i["text"]) for i in core)}, errors={errors}')
            plan=(planner or build_editorial_plan)(materials)
            writer_plan,_=project_editorial_plan(plan)
            # Source IDs are aligned with the planner's source ordinals.
            topic_sources=[]
            reports=[i for i in materials['items'] if i.get('source_genre')!='community_post']
            for item in plan['selection_report']:
                report=reports[item['source_ordinal']-1]
                topic_sources.append({k:report.get(k) for k in ['id','title','url','source','published_at']})
            edition={'version':1,'date':day,'news_cutoff_at':datetime(target_day.year,target_day.month,target_day.day,22,tzinfo=TZ).isoformat(),
                'frozen_at':now.isoformat(),'selection':selection,'source_capture_at':selection.get('source_capture_at'),'materials':materials,'editorial_plan':plan,
                'writer_plan':writer_plan,'topic_sources':topic_sources,'facts_sha256':digest(writer_plan)}
            edition['edition_id']=digest(edition)
            edition['edition_path']=str(path.resolve())
            write_json(path,edition)
            return edition
        except Exception as e:
            write_json(failed,{'date':day,'status':'blocked','error_type':type(e).__name__,'message':str(e)[:300]})
            raise


def podcast_payload(edition):
    return {**edition['materials'],'shared_editorial_plan':edition['editorial_plan'],
        'shared_edition':{k:edition[k] for k in ['edition_id','date','news_cutoff_at','frozen_at','facts_sha256']},
        'shared_edition_path':edition['edition_path']}


def analysis_input(analyzer,edition,prepared_type):
    rows=[]
    for topic,source in zip(edition['writer_plan']['topics'],edition['topic_sources']):
        fact_text='\n'.join(topic['facts'])
        item={**source,'title':topic['facts'][0],'summary':fact_text}
        rows.append(analyzer._register_evidence(item,source['source'],fact_text))
    text=json.dumps({k:edition[k] for k in ['edition_id','news_cutoff_at','facts_sha256']},ensure_ascii=False)+'\n'+'\n'.join(rows)
    return prepared_type(text,'',len(rows),0,len(rows),len(rows),0)


def analysis_once(edition,messages,client):
    directory=Path(edition['edition_path']).parent
    with locked(directory/'.analysis.lock'):
        raw=directory/'analysis-response.json';attempt=directory/'analysis-attempt.json'
        if raw.exists():return json.loads(raw.read_text())['response']
        if attempt.exists():raise ValueError('analysis request already attempted; no second model call')
        write_json(attempt,{'status':'started','edition_id':edition['edition_id'],'prompt_sha256':digest(messages)})
        response=client.chat(messages,num_retries=0)
        write_json(raw,{'response':response,'edition_id':edition['edition_id']})
        return response


def capture_sources(repo,now=None,settings=None):
    """Obtain source text before cutoff; no writing call and no freeze yet."""
    now=(now or datetime.now(TZ)).astimezone(TZ)
    if not (21<=now.hour<22):raise ValueError('source capture is limited to21:00-21:59')
    repo=Path(repo);day=now.date().isoformat();directory=repo/'TrendRadar/output/meta/daily_editions'/day
    if str(repo/'scripts') not in sys.path:sys.path.insert(0,str(repo/'scripts'))
    from generate_site_insights import load_articles
    from podcast_materials import build_materials
    with locked(directory/'.capture.lock'):
        if (directory/'edition.json').exists():raise ValueError('edition already frozen')
        articles,_=load_articles(repo/'TrendRadar/output')
        # capture moment rather than future cutoff limits source eligibility.
        selected=cutoff_candidates(repo,day,articles)
        settings=settings or {}
        ordered=ranked_candidates(selected,now.date())[:18]
        started=time.monotonic();items=[];reviews=[]
        for offset in range(0,len(ordered),6):
            remaining=45-(time.monotonic()-started)
            if remaining<2:break
            part,review=build_materials(repo,now.date(),directory/'source-cache',candidates=ordered[offset:offset+6],
                max_items=6,max_fetch=6,max_age_days=int(settings.get('max_age_days',3)),budget_seconds=remaining)
            items.extend(part['items']);reviews.append(review)
        payload={'schema_version':1,'policy_version':'podcast-materials-v1','date':day,'items':items,'source_materials_verified':False}
        review={'batches':reviews,'candidate_superset_limit':18,'captured_items':len(items)}
        finished=datetime.now(TZ)
        if finished.date()!=now.date() or finished.hour>=22:raise ValueError('source capture crossed22:00; prior capture retained')
        path=directory/'source-capture.json'
        if path.exists():
            previous=json.loads(path.read_text())
            if digest(previous['materials'])!=previous['materials_sha256']:raise ValueError('previous source capture integrity mismatch')
            merged={i['url']:i for i in previous['materials']['items']}
            merged.update({i['url']:i for i in payload['items']})
            payload['items']=list(merged.values())
        record={'captured_at':finished.isoformat(),'materials':payload,'materials_sha256':digest(payload)}
        write_json(directory/('source-capture-'+finished.strftime('%H%M%S')+'.json'),record)
        write_json(path,record)
        write_json(directory/'capture-review.json',review)
        return path
