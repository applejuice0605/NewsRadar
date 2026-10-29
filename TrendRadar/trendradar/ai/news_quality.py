"""Conservative title-level exclusions; raw collection remains archived."""
from __future__ import annotations
import re


def peripheral_reason(title: str) -> str | None:
    text = title or ''
    if re.search(r'走私|查获.{0,20}(?:芯片|服务器)|(?:芯片|服务器).{0,20}被扣', text):
        return 'smuggling_incident'
    if re.search(r'公关负责人|传言严重失实|(?:回应|辟谣).{0,30}(?:传言|群演|肖像|声音)|营销号|违规账号|恶意营销', text):
        return 'pr_or_platform_moderation'
    if re.search(r'培训课|课程.{0,8}(?:优惠|限时)|扫码.{0,8}(?:报名|领取)|广告推广|付费推广|软文', text):
        return 'promotion'
    return None
