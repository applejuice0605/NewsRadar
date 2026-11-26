"""Conservative title-level exclusions; raw collection remains archived."""
from __future__ import annotations
import re
import math


def peripheral_reason(title: str) -> str | None:
    text = title or ''
    if re.search(r'走私|查获.{0,20}(?:芯片|服务器)|(?:芯片|服务器).{0,20}被扣', text):
        return 'smuggling_incident'
    if re.search(r'公关负责人|传言严重失实|(?:回应|辟谣).{0,30}(?:传言|群演|肖像|声音)|营销号|违规账号|恶意营销', text):
        return 'pr_or_platform_moderation'
    if re.search(r'培训课|课程.{0,8}(?:优惠|限时)|扫码.{0,8}(?:报名|领取)|广告推广|付费推广|软文', text):
        return 'promotion'
    return None


def title_evidence_eligible(title, assessment):
    """Shared gate for recommendations and new podcast source selection."""
    if not isinstance(assessment, dict) or assessment.get('completed') is not True or assessment.get('decision') != 'accept':
        return False
    if assessment.get('policy_version') in {None, 'legacy_unknown'}:
        return False
    for key, minimum in [('topic_relevance', .7), ('information_value', .5)]:
        value = assessment.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= 1:
            return False
    quote = assessment.get('evidence_quote')
    return isinstance(quote, str) and bool(quote.strip()) and quote in (title or '') and not peripheral_reason(title)
