"""Title-only editorial assessment. No database writes or keyword vetoes.

Scores describe classifier judgments, never ground truth. A completed rejection is
not a request failure. Policy changes do not invalidate the legacy analyzed cache.
"""
import json
import math
from pathlib import Path
import tempfile

POLICY_VERSION = 'editorial-v2'
LEGACY_VERSION = 'legacy_unknown'
NEW_FIELDS = {'topic_relevance', 'information_value', 'decision', 'reason', 'evidence_quote'}


def score(value):
    """Strict JSON number in [0, 1]; bool, strings, NaN and infinities are invalid."""
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError('score must be a finite number in [0,1]')
    return float(value)


def thresholds(config):
    return (score(config.get('MIN_TOPIC_RELEVANCE', .7)),
            score(config.get('MIN_INFORMATION_VALUE', .5)))


def unknown(item_id, reason='missing_decision', status='request_failure'):
    return dict(id=item_id, tag_id=None, topic_relevance=None, information_value=None,
                decision=None, reason=reason, reason_code=reason, evidence_quote='',
                status=status, completed=False, policy_version=POLICY_VERSION)


def legacy_assessment(item_id, tag_id=None, relevance=None, decision=None):
    return dict(id=item_id, tag_id=tag_id, topic_relevance=relevance,
                information_value=None, decision=decision, reason='legacy_unknown: value and evidence unavailable',
                reason_code=LEGACY_VERSION, evidence_quote='', status=LEGACY_VERSION,
                completed=decision is not None, policy_version=LEGACY_VERSION)


def parse_decisions(data, titles, tags, config=None):
    """Return exactly one assessment per input, including invalid/missing decisions.

    Legacy sparse arrays remain readable, but omitted inputs are retryable unknowns.
    Explicit legacy empty tags mean reject. New duplicate decisions fail that item.
    """
    min_topic, min_value = thresholds(config or {})
    valid_tags = {t['id'] for t in tags if type(t.get('id')) is int}
    valid_ids = {t['id'] for t in titles if type(t.get('id')) is int}
    grouped = {i: [] for i in valid_ids}
    if not isinstance(data, list):
        return [dict(unknown(t['id'], 'invalid_response'), title=t['title'], tag=None) for t in titles]
    for entry in data:
        if isinstance(entry, dict) and type(entry.get('id')) is int and entry['id'] in valid_ids:
            grouped[entry['id']].append(entry)
    assessments = []
    for title in titles:
        item_id = title['id']
        entries = grouped.get(item_id, [])
        if not entries:
            assessments.append(unknown(item_id))
            continue
        try:
            is_new = any(NEW_FIELDS.intersection(e) for e in entries)
            if is_new:
                if len(entries) != 1:
                    raise ValueError('duplicate_decision')
                e = entries[0]
                if not (NEW_FIELDS | {'id', 'tag_id'}).issubset(e):
                    raise ValueError('incomplete_decision')
                topic, value = score(e['topic_relevance']), score(e['information_value'])
                tag_id, decision = e['tag_id'], e['decision']
                if tag_id is not None and (type(tag_id) is not int or tag_id not in valid_tags):
                    raise ValueError('invalid_tag_id')
                if decision not in ('accept', 'reject') or not isinstance(decision, str):
                    raise ValueError('invalid_decision')
                if not isinstance(e['reason'], str) or not e['reason'].strip():
                    raise ValueError('missing_reason')
                quote = e['evidence_quote']
                if not isinstance(quote, str):
                    raise ValueError('invalid_evidence_quote')
                if quote and (not quote.strip() or quote not in title['title']):
                    raise ValueError('evidence_not_in_title')
                if (decision == 'accept' or value >= min_value) and not quote.strip():
                    raise ValueError('missing_value_evidence')
                if decision == 'accept' and tag_id is None:
                    raise ValueError('missing_accept_tag')
                code = 'model_accept' if decision == 'accept' else 'model_reject'
                if decision == 'accept' and topic < min_topic:
                    decision, code = 'reject', 'insufficient_topic_relevance'
                elif decision == 'accept' and value < min_value:
                    decision, code = 'reject', 'insufficient_information_value'
                reason = e['reason'].strip()
                if code.startswith('insufficient_'):
                    reason = code + ': ' + reason
                assessments.append(dict(id=item_id, tag_id=tag_id,
                    topic_relevance=topic, information_value=value, decision=decision,
                    reason=reason, reason_code=code, evidence_quote=quote,
                    status='assessed', completed=True, policy_version=POLICY_VERSION))
            else:
                candidates = []
                explicit_empty = False
                for e in entries:
                    if 'tag_id' in e:
                        matches = [e]
                    elif isinstance(e.get('tags'), list):
                        matches = e['tags']
                        explicit_empty |= not matches
                    else:
                        raise ValueError('invalid_legacy_decision')
                    for match in matches:
                        if not isinstance(match, dict):
                            raise ValueError('invalid_legacy_tag')
                        tid = match.get('tag_id')
                        if type(tid) is not int or tid not in valid_tags:
                            raise ValueError('invalid_tag_id')
                        candidates.append((score(match.get('score', .5)), tid))
                if candidates:
                    relevance, tid = max(candidates, key=lambda c: c[0])
                    assessments.append(legacy_assessment(item_id, tid, relevance, 'accept'))
                elif explicit_empty:
                    assessments.append(legacy_assessment(item_id, decision='reject'))
                else:
                    raise ValueError('invalid_legacy_decision')
        except (ValueError, TypeError, OverflowError) as exc:
            assessments.append(unknown(item_id, str(exc)))
    tag_names = {t['id']: t.get('tag') for t in tags if type(t.get('id')) is int}
    for assessment, title in zip(assessments, titles):
        assessment['title'] = title['title']
        assessment['tag'] = tag_names.get(assessment['tag_id'])
    return assessments


def write_sidecar(data_dir, day, run):
    """Append a run atomically under <local data_dir>/meta/filter_quality/YYYY-MM-DD.json.

    Caller must select a known local backend, never infer a private DB path. Errors
    propagate so caller can expose them in result. Runs retain retries and rejects.
    """
    from datetime import date
    day = date.fromisoformat(day).isoformat()
    path = Path(data_dir) / 'meta' / 'filter_quality' / (day + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    # Serialize concurrent writers without a database or third-party dependency.
    import fcntl
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        document = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
            'schema_version': 1, 'date': day, 'runs': []}
        if not isinstance(document, dict) or not isinstance(document.get('runs'), list):
            raise ValueError('invalid_existing_sidecar')
        document['runs'].append(run)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile('w', dir=path.parent, encoding='utf-8', delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(document, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write('\n')
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return str(path)
