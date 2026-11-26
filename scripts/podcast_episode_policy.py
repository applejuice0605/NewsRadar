"""Deterministic dialogue slots and the shorter, source-only bulletin policy."""
from __future__ import annotations

from datetime import datetime, time
import re
from zoneinfo import ZoneInfo

BULLETIN_MIN_SECONDS = 90
BULLETIN_MIN_CHARS = 500
SLOT_COUNT = 36
SLOT_TARGET = 60


def writing_seconds(day) -> float:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    if now.date() != day:
        return 600
    return max(0, (datetime.combine(day, time(22, 32), now.tzinfo) - now).total_seconds())


def slot_instruction(topic_count: int) -> str:
    rows = []
    for slot in range(1, SLOT_COUNT + 1):
        speaker = "S1" if slot % 2 else "S2"
        if slot <= 4:
            role = "开场"
        elif slot >= 35:
            role = "收尾"
        else:
            topic = 1 + (slot - 5) * max(topic_count, 1) // 30
            role = f"选题{topic}（{'追问或补充' if speaker == 'S2' else '展开事实'}）"
        rows.append(f"{slot:02d} {speaker} {role}")
    return "节目分配（只输出编号与台词，不输出说话人或角色）：\n" + "\n".join(rows)


def read_slots(raw: str, allowed: set[int]) -> dict[int, str]:
    """Read an unambiguous subset. Blank slots are treated as missing."""
    from podcast_dialogue import DialogueError
    rows = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"\s*([0-9]{2})[:：]\s*(.*?)\s*", line)
        if not match:
            raise DialogueError("expected an assigned slot number and dialogue")
        slot = int(match[1])
        if slot not in allowed or slot in rows:
            raise DialogueError("unexpected or duplicate dialogue slot")
        rows[slot] = match[2]
    return {slot: line for slot, line in rows.items() if line}


def slot_dialogue(rows: dict[int, str]) -> dict:
    from podcast_dialogue import DialogueError, HOSTS, _turns_json_metadata
    if set(rows) != set(range(1, SLOT_COUNT + 1)):
        raise DialogueError("assigned dialogue slots missing")
    turns = [["S1" if slot % 2 else "S2", rows[slot]] for slot in range(1, SLOT_COUNT + 1)]
    return {"hosts": HOSTS, "text": turns, "turn_meta": _turns_json_metadata(turns)}


def bulletin_dialogue(writer_plan: dict, source_text: str) -> tuple[dict, dict]:
    """Read complete, bounded frozen excerpts, retaining source attribution."""
    from podcast_dialogue import DialogueError, HOSTS, _spoken_sentence_units, validate_dialogue
    from generate_dialogue_podcast import normalize_long_semicolon_sentences
    turns = [["S1", "欢迎收听今日热点新闻趋势。接下来是晚间简报。"]]
    used = set()
    for topic in writer_plan["topics"]:
        excerpts = []
        for fact in topic["facts"]:
            for sentence in re.findall(r"[^。！？\n]+[。！？]", fact):
                sentence = sentence.strip()
                if sentence in used or not 20 <= len(sentence) <= 120:
                    continue
                normalized, _ = normalize_long_semicolon_sentences("[01:S1] " + sentence)
                body = normalized.removeprefix("[01:S1] ")
                if any(_spoken_sentence_units(part) > 65 for part in re.split(r"[。！？!?：]", body)):
                    continue
                excerpts.append(body)
                used.add(sentence)
                if len(excerpts) == 5:
                    break
            if len(excerpts) == 5:
                break
        if excerpts:
            speaker = "S2" if len(turns) % 2 else "S1"
            if topic["source"]:
                turns.append([speaker, "以下摘自" + topic["source"] + "的报道。"])
            turns.extend([speaker, excerpt] for excerpt in excerpts)
    turns.append(["S1", "以上是本期晚间简报，感谢收听。"])
    value = {"hosts": HOSTS, "text": turns, "turn_meta": {
        "0": {"topic": "overview", "kind": "overview"},
        **{str(i): {"topic": "discussion", "kind": "continuation"} for i in range(1, len(turns) - 1)},
        str(len(turns) - 1): {"topic": "closing", "kind": "closing"},
    }}
    result, review = validate_dialogue(value, source_text=source_text, enforce_length=True, bulletin=True)
    if review["warnings"]:
        raise DialogueError("frozen excerpts did not pass bulletin review")
    return result, review
