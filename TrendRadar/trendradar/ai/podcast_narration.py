"""Conservative, offline narration editing with a traceable field ledger.

Only attribution, approved caveat phrases and punctuation are edited. This is
not a factual verifier, a free-form paraphraser or an automatic listening test.
"""
from __future__ import annotations

import hashlib
import re
from typing import Callable

NARRATION_VERSION = "oral-v1"
_PROTECTED_PATTERN = r"[A-Za-z][A-Za-z0-9]*(?:[._+/-][A-Za-z0-9]+)*|\d+[A-Za-z][A-Za-z0-9]*|[+−-]?\d+(?:[.,]\d+)*(?:%|％)?"
_CAVEAT_PREFIXES = (
    ("仅有标题，未核验正文；", "目前只拿到了标题，还没有核对正文。"),
    ("仅依据输入标题或摘要，未独立核验。", "这些信息来自标题或摘要，还没有独立核实。"),
    ("未提供正文或官方材料，", "目前还没有正文或官方材料。"),
    ("仅有标题，未见官方发布材料；", "目前只拿到了标题，还没有看到官方发布材料。"),
    ("仅有标题，未提供官方公告、正文或可核验的模型规格；", "目前只有标题，还没有官方公告、正文或可核验的模型规格。"),
)


def _caveat(text: str) -> str:
    # Prefix-only: never rewrite these words inside a quotation or condition.
    for old, new in _CAVEAT_PREFIXES:
        if text.startswith(old):
            return new + text[len(old):]
    return text


def _rhythm(text: str) -> str:
    """Break explicit clause boundaries, leaving quotes/conditions untouched."""
    result = []
    stack = []
    closing = {"“": "”", "‘": "’", "（": "）", "(": ")", "[": "]", "【": "】"}
    sentence = ""
    for position, char in enumerate(text):
        if char == '"':
            if stack and stack[-1] == char:
                stack.pop()
            else:
                stack.append(char)
        elif char in closing:
            stack.append(closing[char])
        elif stack and char == stack[-1]:
            stack.pop()
        conditional = re.search(r"如果|假如|倘若|假设|要是|只要|只有|除非|一旦|当|若|在.+(?:时|前|后)|(?i:\b(?:if|unless|when|provided|assuming)\b)", sentence)
        explicit_restart = re.match(r"(?:不能据此|无法据此|还不能|目前无法|仍需|再评估|工程侧|工程团队|适合从)", text[position + 1:])
        # A semicolon alone does not prove independent scope. Only known
        # discourse restarts may become a sentence break; leave the rest alone.
        if not stack and not conditional and char in "；;，," and explicit_restart:
            char = "。"
        result.append(char)
        sentence = "" if char in "。！？!?" and not stack else sentence + char
    return "".join(result)


def _sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    return text if text[-1] in "。！？!?" or re.search(r"[。！？!?][”’]$", text) else text + "。"


def _words(text: str) -> str:
    # Chinese names, negations, modality, Latin identifiers, digits and units
    # all remain in order; only punctuation/spacing may vary.
    return "".join(re.findall(r"\w", text, re.UNICODE))


def _protected(text: str) -> list[str]:
    return re.findall(_PROTECTED_PATTERN, text)


def validate_ledger(ledger: list[dict]) -> None:
    for unit in ledger:
        source = unit["source_text"]
        expected = _caveat(source) if unit["role"] == "uncertainty" else source
        if (_words(expected) != _words(unit["spoken_text"])
                or _protected(expected) != _protected(unit["spoken_text"])):
            raise ValueError("narration changed source words, numbers or qualifiers")


def compose_narration(pack: dict, sanitize: Callable[[str], str]) -> tuple[str, dict]:
    """Render validated material; never call a model, pad or truncate facts."""
    ledger, paragraphs = [], []

    def field(text: str, role: str, index: int) -> str:
        source = sanitize(text)
        if not source.strip():
            raise ValueError("no speakable text in narration field")
        edited = _caveat(source) if role == "uncertainty" else source
        spoken = _sentence(_rhythm(edited))
        ledger.append({"topic": index, "role": role, "source_text": source, "spoken_text": spoken})
        return spoken

    claims = pack["claims"]
    records = claims or pack["articles"]
    for index, record in enumerate(records):
        if claims:
            sources = [pack["evidence_sources"][key] for key in record["source_ids"]]
            names = list(dict.fromkeys(sanitize(s.get("source", "")) or "所引新闻来源" for s in sources))
            fact_text = sanitize(record["fact"])
            title_only = any(fact_text in sanitize(s["title"]) for s in sources)
            full_title = all(fact_text == sanitize(s["title"]) for s in sources)
            fact = field(record["fact"], "fact", index)
        else:
            sources = [record]
            names = [sanitize(record.get("source") or record.get("source_name", "")) or "所引新闻来源"]
            title_only = True
            full_title = True
            fact = field(record["title"], "fact", index)
        # A title-only headline is attributed as a headline, not a verified event.
        if title_only:
            if full_title:
                paragraph = f"{'、'.join(names)}有一篇文章，标题是：“{fact}”"
            else:
                # A validated claim may be a title fragment, not the whole
                # title. Mixed citations need existential, not universal framing.
                paragraph = f"{'、'.join(names)}提供的材料中，有文章的标题提到：“{fact}”"
            paragraph = _sentence(paragraph)
        else:
            paragraph = f"据{'、'.join(names)}报道，{fact}"
        if claims:
            if record.get("interpretation", "").strip():
                interpretation = record["interpretation"]
                # Preserve explicit source qualifications instead of reciting
                # the same generic disclaimer before every conditional analysis.
                qualified = re.search(r"如果|若|可能|应先|不应|可考虑|可优先", sanitize(interpretation))
                if not qualified:
                    paragraph += "一种还需验证的看法是，"
                paragraph += field(interpretation, "interpretation", index)
            paragraph += "不过，" + field(record.get("uncertainty", "").strip()
                                       or "仅依据输入标题或摘要，未独立核验。", "uncertainty", index)
        elif record.get("summary", "").strip():
            title, summary = sanitize(record["title"]), sanitize(record["summary"])
            # Exact duplication only. Never use semantic similarity to omit text.
            if title.rstrip("。！？!?") != summary.rstrip("。！？!?"):
                paragraph += "文中还提到，" + field(record["summary"], "summary", index)
        paragraphs.append(paragraph)
    validate_ledger(ledger)
    script = "\n\n".join(paragraphs)
    if not script.strip():
        raise ValueError("no speakable source material")
    warnings = []
    for index, paragraph in enumerate(paragraphs):
        for clause in re.split(r"[。！？!?：]", paragraph):
            if len(clause) > 65:
                warnings.append({"topic": index, "kind": "long_sentence",
                                 "text": clause, "note": "保留语义，需人工检查换气与理解负担"})
    terms = list(dict.fromkeys(_protected(script)))
    report = {"version": NARRATION_VERSION, "mode": "offline_rules", "additional_model_calls": 0,
              "passes": ["cleanup", "exact_duplicate_removal", "punctuation_rhythm"],
              "field_words_preserved": True, "factual_truth_verified": False,
              "listening_review_required": True, "warnings": warnings,
              "pronunciation_review_terms": terms, "ledger": ledger,
              "estimated_seconds_at_280_chars_per_minute": round(len(script) * 60 / 280, 1),
              "script_sha256": hashlib.sha256(script.encode()).hexdigest()}
    return script, report
