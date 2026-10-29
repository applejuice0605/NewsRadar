"""Validate extractive evidence against the actual input, never model-provided URLs."""
from urllib.parse import urlsplit

SECTIONS = ('core_trends', 'sentiment_controversy', 'signals', 'rss_insights', 'outlook_strategy')


def safe_source_url(value):
    if not isinstance(value, str) or any(c.isspace() for c in value):
        return ''
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username and not parsed.password else ''
    except ValueError:
        return ''


def ground_analysis(result, sources):
    """Only publish claims quoting input text and citing known, accessible sources.

    This verifies provenance/extractiveness, NOT the truth of a source or inference.
    Legacy reports can still be parsed, but new analyses must pass this gate.
    """
    accepted, rejected = [], 0
    grouped = {section: [] for section in SECTIONS}
    for claim in result.evidence_claims[:20]:
        if not isinstance(claim, dict):
            rejected += 1
            continue
        section = claim.get('section')
        ids = claim.get('source_ids')
        fact = claim.get('fact')
        if (not isinstance(section, str) or section not in grouped or not isinstance(ids, list) or not ids
                or not all(isinstance(i, str) and i in sources and sources[i]['url'] for i in ids)
                or not isinstance(fact, str) or not fact.strip()):
            rejected += 1
            continue
        fact = fact.strip()
        ids = list(dict.fromkeys(ids))
        # A verbatim excerpt avoids accepting invented numbers as "facts".
        ids = [i for i in ids if fact in sources[i]['title'] or fact in sources[i].get('summary', '')]
        if len(fact) < 6 or not ids:
            rejected += 1
            continue
        interpretation = claim.get('interpretation', '')
        uncertainty = claim.get('uncertainty', '')
        if not isinstance(interpretation, str) or not isinstance(uncertainty, str):
            rejected += 1
            continue
        entry = {'section': section, 'fact': fact, 'source_ids': ids,
                 'interpretation': interpretation[:600], 'uncertainty': uncertainty[:300]}
        accepted.append(entry)
        citation = ' '.join('[' + i + ']' for i in ids)
        text = '事实摘录（来源陈述，未独立核验）：' + fact + ' ' + citation
        if interpretation.strip():
            text += '\nAI 判断（推测）：' + interpretation[:600]
        text += '\n不确定性：' + (uncertainty[:300] or '仅依据输入标题或摘要，未核验全文与独立来源。')
        grouped[section].append(text)
    result.evidence_claims = accepted
    result.evidence_sources = {i: sources[i] for claim in accepted for i in claim['source_ids']}
    result.evidence_rejected_count = rejected
    for section in SECTIONS:
        setattr(result, section, '\n\n'.join(grouped[section]))
    # Free-form source summaries must not bypass the same provenance gate.
    result.standalone_summaries = {}
    if not accepted:
        result.success = False
        result.error = '未返回通过来源校验的分析，保留原始新闻；不展示无依据结论'
    return result
