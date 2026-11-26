#!/usr/bin/env python3
"""Build a deterministic, local editorial outline from frozen source material.

This module is deliberately an *editorial preflight*, not a writer.  It never
calls a model, changes source text, or invents a fact.  A fact is emitted only
when it is an exact sentence substring of the input report body.
"""

from __future__ import annotations

import importlib.util
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Mapping


VERSION = "editorial-plan-v2-dialogue-brief"
_SENTENCE_END = "。！？；!?;"
_CJK = re.compile(r"[\u3400-\u9fff]")

# These are deliberately conservative editorial filters.  They remove the
# tail of an article that tends to be company/market reporting, while leaving
# a lead sentence alone when it is the news event itself.
_TAIL_NOISE = re.compile(
    r"融资|估值|年化(?:收入|营收)?|收入运行率|营收|股权|投资者|融资传闻|"
    r"客户[^。！？；!?;]{0,20}(?:名单|清单|列表|包括|有|数量|人数|达到)|"
    r"(?:员工|雇员)[^。！？；!?;]{0,20}(?:名单|清单|列表|数量|人数|达到)|"
    r"价格|售价|套餐|榜单|排名|排行|市值|倍数|美元融资|元融资|revenue|valuation|"
    r"funding|financing|investors?|arr\b|price|pricing|"
    r"customers?\s+(?:include|such as|list|count)|employees?\s+(?:include|list|count)|"
    r"rank(?:ed|s|ing)?|leaderboard|multiple|million (?:input|output) tokens",
    re.I,
)
_STAT_OR_SCORE_TAIL = re.compile(
    r"(?:\b\d+(?:\.\d+)?\s*(?:倍|million|billion)|"
    r"(?:调查|survey|预算|支出|spending).{0,100}\b\d+(?:\.\d+)?\s*%|"
    r"\b\d+(?:\.\d+)?\s*%[^。.!?；;]{0,100}(?:调查|survey|预算|支出|spending)|"
    r"benchmark|榜单|排名|排行|leaderboard|#\d|\$\s*\d|价格|pricing|price|"
    r"年化收入|收入运行率|revenue|arr\b|美元融资|元融资)",
    re.I,
)
_QUALIFIER = re.compile(
    r"数据来自|不代表|没有据此|衡量对象|specific survey|not representative|"
    r"does not mean|not a claim|not all (?:companies|projects)|does not measure",
    re.I,
)
_MECHANISM = re.compile(
    r"位于[^。！？；!?;]{0,80}之间|根据[^。！？；!?;]{0,60}(?:需求|实际需求)[^。！？；!?;]{0,80}调整|"
    r"自动(?:完成|扩缩|调整|伸缩|配置)|扩缩[^。！？；!?;]{0,40}(?:资源|计算)|监控[^。！？；!?;]{0,50}(?:表现|运行)|"
    r"释放[^。！？；!?;]{0,50}时间[^。！？；!?;]{0,50}(?:创新|成果|增长)|"
    r"把[^。！？；!?;]{0,40}时间[^。！？；!?;]{0,50}(?:创新|成果|增长)|"
    r"located between|adjust(?:s|ed)?[^.?!;]{0,100}according to|automatically\s+(?:scale|adjust|configure)|"
    r"scale[^.?!;]{0,70}resources|monitor[^.?!;]{0,70}(?:performance|running)|"
    r"free[^.?!;]{0,50}time|time[^.?!;]{0,60}(?:innovation|outcomes|growth)|"
    r"sustained\s+agentic|run\s+for\s+days|multi-step\s+engineering|root-caus(?:e|ing)[^,.;]{0,60}patch",
    re.I,
)
_LIMITATION = re.compile(
    r"短板|限制|还没有找到|偏移|不稳定|仍处|guardrail|limitation|shortcoming|"
    r"not yet|before making|feedback and iterate|misalignment|fabrication",
    re.I,
)
_OPENING = re.compile(
    r"研究预览|测试者|可信|逐步|陆续|未来|开放|可用|上线|research preview|trusted|"
    r"subset|rollout|roll out|available|access|gradually|early",
    re.I,
)
_SPECIFIC_OPENING = re.compile(
    r"subset|available\s+to|starting\s+(?:this\s+week|today)|trusted\s+(?:tester|partner)|"
    r"一部分|首批|可信测试者|可信合作方|仅向|先向",
    re.I,
)
_AVAILABILITY = re.compile(
    r"研究预览|测试者|可信|逐步|陆续|未来|开放|可用|上线|限制|短板|仍处|还没有|"
    r"research preview|trusted|subset|rollout|roll out|available|access|"
    r"gradually|before making|guardrail|early|limitation|shortcoming|not yet",
    re.I,
)
_CAPABILITY = re.compile(
    r"能够|支持|打通|共享|生成|整合|调用|自动|优化|检索|接入|运行|协同|"
    r"can |supports?|integrat(?:e|ed|ion)|generate|retrieve|connect|run|"
    r"capabilit(?:y|ies)|cybersecurity|coding|agentic|patch|workflow",
    re.I,
)
_LEAD_META = re.compile(r"^(?:today|breaking|source:|来源：|责任编辑|it之家\s+\d+月)", re.I)
_ITHOME_PREFIX = re.compile(r"^\s*IT之家\s*\d+\s*月\s*\d+\s*日消息[，,]\s*", re.I)


def _source_genre(item: Mapping[str, Any]) -> str:
    """Reuse podcast_materials' publisher classifier without requiring a package."""
    try:
        from podcast_materials import _source_genre as classifier
    except (ImportError, ModuleNotFoundError):
        spec = importlib.util.spec_from_file_location(
            "newsradar_podcast_materials_for_editorial_plan",
            Path(__file__).with_name("podcast_materials.py"),
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("podcast material genre classifier is unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        classifier = module._source_genre
    return str(classifier(dict(item)))


def _split_chinese(text: str) -> list[str]:
    parts: list[str] = []
    start = 0
    for index, char in enumerate(text):
        if char in _SENTENCE_END:
            candidate = text[start : index + 1].strip()
            if candidate:
                parts.append(candidate)
            start = index + 1
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _split_english(text: str) -> list[str]:
    """Split prose at sentence periods, without splitting versions/decimals.

    A period is a boundary only when followed by whitespace and an uppercase
    letter (or a quote/bracket followed by one).  Thus ``ML4/5.9.0`` and
    ``5.9.0`` remain intact.  Question/exclamation marks are always boundaries.
    """
    parts: list[str] = []
    start = 0
    index = 0
    while index < len(text):
        char = text[index]
        boundary = char in "!?"
        if char == ".":
            rest = text[index + 1 :]
            match = re.match(r"\s+(?:[\"'\u2019\u201d\)\]]\s*)?([A-Z])", rest)
            boundary = bool(match)
        if boundary:
            candidate = text[start : index + 1].strip()
            if candidate:
                parts.append(candidate)
            index += 1
            start = index
            continue
        index += 1
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _sentences(text: str) -> list[str]:
    if not isinstance(text, str):
        return []
    chinese = len(_CJK.findall(text))
    latin = len(re.findall(r"[A-Za-z]", text))
    return _split_chinese(text) if chinese >= latin else _split_english(text)


def _similar(left: str, right: str) -> bool:
    if left == right:
        return True
    shorter, longer = sorted((left, right), key=len)
    if len(shorter) >= 24 and shorter in longer:
        return True
    return SequenceMatcher(None, left, right, autojunk=False).ratio() >= 0.88


def _clean_fact(sentence: str) -> str:
    """Drop outlet boilerplate/leading quote while retaining an exact substring."""
    cleaned = _ITHOME_PREFIX.sub("", sentence)
    return cleaned.lstrip(" \t\r\n\"'“‘”’")


def _is_tail_noise(sentence: str, ordinal: int) -> bool:
    # The lead is the event and must survive even if the event is a financing
    # announcement.  Subsequent company/market details are not core facts.
    if ordinal <= 0 or _QUALIFIER.search(sentence):
        return False
    # A concrete mechanism remains useful even when the source reports a
    # percentage alongside it (for example, time reallocated to innovation).
    if _MECHANISM.search(sentence):
        return False
    return bool(_TAIL_NOISE.search(sentence) or _STAT_OR_SCORE_TAIL.search(sentence))


def _category(sentence: str) -> str:
    if _MECHANISM.search(sentence):
        return "mechanism"
    if _LIMITATION.search(sentence):
        return "limitation"
    if _OPENING.search(sentence):
        return "availability_or_limit"
    if _CAPABILITY.search(sentence):
        return "capability"
    return "context"


def _score(sentence: str, ordinal: int) -> float:
    value = 0.0
    if ordinal == 0:
        value += 6.0
    if _MECHANISM.search(sentence):
        value += 9.0
    if _CAPABILITY.search(sentence):
        value += 4.0
    if _LIMITATION.search(sentence):
        value += 3.0
    if _OPENING.search(sentence):
        value += 2.0
    if _SPECIFIC_OPENING.search(sentence):
        value += 5.0
    if len(sentence) >= 25:
        value += 1.0
    if len(sentence) > 900:
        value -= 1.0
    return value


def _deduped_candidates(sentences: Iterable[str]) -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []
    for ordinal, sentence in enumerate(sentences):
        compact = re.sub(r"\s+", "", sentence)
        if re.match(r"感谢.*网友.*线索投递", compact):
            continue
        if len(compact) < 6 or re.fullmatch(r"(?:导航|登录|注册|目录|菜单|下一页|上一页)+[。.!！；;]?", compact):
            continue
        if any(_similar(sentence, previous) for _, previous in result):
            continue
        if _QUALIFIER.search(sentence) and any(_QUALIFIER.search(previous) for _, previous in result):
            continue
        result.append((ordinal, sentence))
    return result


def _select_facts(text: str) -> tuple[list[str], int]:
    sentences = _sentences(text)
    candidates = _deduped_candidates(sentences)
    if not candidates:
        return [], 0
    # A Chinese news lead is mandatory.  For English, a boilerplate “Today…”
    # lead may be skipped when there are substantive sentences after it.
    is_chinese = len(_CJK.findall(text)) >= len(re.findall(r"[A-Za-z]", text))
    selected: list[tuple[int, str]] = []
    if candidates and (is_chinese or not _LEAD_META.match(candidates[0][1])):
        selected.append(candidates[0])

    usable = [item for item in candidates if item not in selected and not _is_tail_noise(item[1], item[0])]
    # Cover the mechanism first, then at most one opening and one limitation.
    # This prevents a rollout paragraph from crowding out how the thing works.
    for wanted, limit in (("mechanism", 2), ("capability", 1), ("availability_or_limit", 1), ("limitation", 1)):
        pool = [item for item in usable if _category(item[1]) == wanted]
        for _ in range(limit):
            if not pool or len(selected) >= 5:
                break
            chosen = max(pool, key=lambda item: (_score(item[1], item[0]), -item[0]))
            selected.append(chosen)
            usable.remove(chosen)
            pool.remove(chosen)
    remaining = sorted(usable, key=lambda item: (-_score(item[1], item[0]), item[0]))
    for item in remaining:
        if len(selected) >= 5:
            break
        selected.append(item)

    # If filters leave fewer than three, use the earliest remaining sentences
    # rather than manufacturing a connective sentence.  A genuinely empty
    # source still fails loudly below.
    if len(selected) < 3:
        # Never re-introduce an explicitly excluded finance/statistics tail just
        # to meet the preferred sentence count.
        fallback = [item for item in candidates
                    if item not in selected and not _is_tail_noise(item[1], item[0])]
        for item in fallback:
            if len(selected) >= min(3, len(candidates)):
                break
            selected.append(item)
    # Keep the writing hand-off in editorial order: lead, mechanism/capability,
    # one opening/limit, then any context.  Sentences within a class retain
    # their original source order.
    category_order = {
        "mechanism": 1,
        "capability": 2,
        "availability_or_limit": 3,
        "limitation": 4,
        "context": 5,
    }
    selected.sort(key=lambda item: (0 if item[0] == candidates[0][0] else category_order.get(_category(item[1]), 6), item[0]))
    return [_clean_fact(sentence) for _, sentence in selected[:5]], max(0, len(sentences) - len(selected))


def _question_seed(text: str) -> str:
    if re.search(r"腾讯文档|腾讯会议|网盘|办公|gmail|google drive|drive|日历|office|插件", text, re.I):
        return "把分散在邮件、文档和会议里的信息接起来后，人实际省掉了哪一步？"
    if re.search(r"虚拟世界|世界模型|world model|simulation|simulated", text, re.I):
        return "看一段生成的视频，和自己能进去改场景，差别在哪里？"
    if re.search(r"时间|组织|生产力|创新|新成果|增长", text, re.I) and re.search(
        r"成本|节约|收益|cost|saving|productivity", text, re.I
    ):
        return "AI省下来的时间，为什么不自动等于公司省下钱？"
    if re.search(r"gpu|算力|计算资源|compute|inference", text, re.I):
        return "软件怎样根据任务需要，在速度、资源和成本之间做安排？"
    if re.search(r"long[- ]horizon|run\s+for\s+days|sustained\s+agentic|持续[^。！？；!?;]{0,20}工程", text, re.I):
        return "把一个复杂任务交给它持续推进，和问它几句话，是同一种帮忙吗？"
    if re.search(r"open[- ]?weight|开放权重", text, re.I):
        return "拿得到和跑得动分别需要什么？"
    if re.search(r"授权|oauth|authorization|permission|consent", text, re.I):
        return "少几次操作，会不会也少一道确认？"
    if _AVAILABILITY.search(text):
        return "展示出来和真正能用之间差在哪？"
    return "这次发生了什么变化，和之前相比具体不同在哪？"


def _topic_category(text: str) -> str:
    """Small routing hint only; this is not a source-quality classifier."""
    if re.search(r"腾讯文档|腾讯会议|网盘|办公|gmail|google drive|日历|office|协同|插件", text, re.I):
        return "office"
    if re.search(r"open[- ]?weight|开放权重", text, re.I):
        return "openweights"
    if re.search(r"gpu|成本|账单|算力|费用|cost|billing|compute", text, re.I):
        return "cost"
    if re.search(r"授权|oauth|authorization|permission|consent", text, re.I):
        return "permissions"
    if re.search(r"研究预览|research preview|trusted|rollout|subset|测试者|guardrail|benchmark", text, re.I):
        return "research"
    return "research"


def _dialogue_moves(index: int) -> list[str]:
    moves = [
        "news_change",
        "listener_question",
        "concrete_explanation",
        "everyday_analogy",
        "pushback",
        "callback_or_observation",
    ]
    offset = index % len(moves)
    return moves[offset:] + moves[:offset]


def _audit_text(report_date: str, topics: list[dict[str, Any]]) -> str:
    lines = [f"report_date: {report_date}"]
    for topic in topics:
        lines.append(f"source: {topic['source']}")
        lines.append(f"published_at: {topic['published_at']}")
        lines.extend(f"fact: {fact}" for fact in topic["facts"])
    return "\n".join(lines)


def _report_text(item: Mapping[str, Any]) -> str:
    for key in ("text", "body", "content"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def build_editorial_plan(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Build the deterministic local plan expected by the writing hand-off.

    Upstream validation owns the minimum-report/body gate.  This function
    performs its own source selection and fails if any selected report has no
    usable verbatim facts, rather than silently filling a template.
    """
    if not isinstance(payload, Mapping):
        raise ValueError("payload must be a mapping")
    report_date = str(payload.get("date") or "").strip()
    raw_reports = payload.get("source_reports", payload.get("items"))
    if not isinstance(raw_reports, list):
        raise ValueError("payload.source_reports/items must be a list")
    reports = [item for item in raw_reports if isinstance(item, Mapping) and _source_genre(item) != "community_post"]
    topics: list[dict[str, Any]] = []
    selection_report: list[dict[str, Any]] = []
    for ordinal, item in enumerate(reports):
        if len(topics) >= 3:
            break
        text = _report_text(item)
        source = str(item.get("source") or item.get("source_name") or "").strip()
        published = str(item.get("published_at") or "").strip()
        facts, excluded = _select_facts(text)
        if not facts:
            raise ValueError(f"source report {ordinal + 1} has no qualifying facts")
        topics.append({
            "source": source,
            "published_at": published,
            "facts": facts,
            "category": _topic_category(" ".join(facts)),
            "brief": {
                "news_change": facts[0],
                "mechanism_facts": [f for f in facts if _MECHANISM.search(f) or _CAPABILITY.search(f)],
                "declared_limits": [f for f in facts if _LIMITATION.search(f) or _AVAILABILITY.search(f)],
            },
            "question_seed": _question_seed(" ".join(facts)),
            "dialogue_moves": _dialogue_moves(len(topics)),
        })
        selection_report.append({
            "source_ordinal": ordinal + 1,
            "source": source,
            "input_chars": len(text),
            "selected_chars": sum(len(fact) for fact in facts),
            "excluded_sentence_count": excluded,
        })
    if not topics:
        raise ValueError("no non-community source reports")

    mainline_count = len(topics)
    return {
        "version": VERSION,
        "date": report_date,
        "programme": {
            "opening": "overview",
            "mainlines": [f"mainline_{index}" for index in range(1, mainline_count + 1)],
            "closing": "closing",
            "target_turns": 40,
            "target_chars": [2200, 2400],
        },
        "topics": topics,
        "limitations": [
            "Facts are verbatim source-text excerpts; no independent factual verification was performed.",
            "Community posts are excluded from the core topic selection.",
            "The outline is local editorial metadata and does not contain a generated script or audio.",
        ],
        "selection_report": selection_report,
        "audit_text": _audit_text(report_date, topics),
    }


__all__ = ["build_editorial_plan", "VERSION"]
