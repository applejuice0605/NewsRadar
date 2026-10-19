#!/usr/bin/env python3
"""Generate one natural-language daily podcast from the latest AI report."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
import shutil
import sys
import tempfile
from datetime import date as Date
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import edge_tts
import yaml

from trendradar.ai.client import AIClient
from trendradar.core.loader import load_config


PROJECT_DIR = Path(__file__).resolve().parents[1] / "TrendRadar"
CONFIG_PATH = PROJECT_DIR / "config" / "config.yaml"
LATEST_DAILY = PROJECT_DIR / "output" / "html" / "latest" / "daily.html"
OUTPUT_ROOT = PROJECT_DIR / "output" / "audio"


class AISectionParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = dict(attrs).get("class", "") or ""
        if self.depth:
            if tag == "div":
                self.depth += 1
            if tag in {"p", "li", "h2", "h3", "br"}:
                self.parts.append("\n")
        elif tag == "div" and "ai-section" in classes.split():
            self.depth = 1

    def handle_endtag(self, tag: str) -> None:
        if not self.depth:
            return
        if tag in {"p", "li", "h2", "h3"}:
            self.parts.append("\n")
        if tag == "div":
            self.depth -= 1

    def handle_data(self, data: str) -> None:
        if self.depth:
            self.parts.append(data)

    def text(self) -> str:
        lines = [re.sub(r"\s+", " ", line).strip() for line in "".join(self.parts).splitlines()]
        return "\n".join(line for line in lines if line)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate the daily TrendRadar podcast")
    parser.add_argument("--force", action="store_true", help="regenerate even when the content hash is unchanged")
    parser.add_argument("--input", type=Path, default=LATEST_DAILY, help="daily HTML report to narrate")
    parser.add_argument("--date", help="report date in YYYY-MM-DD (required for a historical input)")
    return parser.parse_args()


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(path)


def extract_ai_content(path: Path) -> str:
    parser = AISectionParser()
    parser.feed(path.read_text(encoding="utf-8", errors="replace"))
    return parser.text()


def clean_script(value: str, max_chars: int) -> str:
    value = re.sub(r"```.*?```", "", value, flags=re.S)
    value = re.sub(r"^\s{0,3}#{1,6}\s*", "", value, flags=re.M)
    value = re.sub(r"^\s*(?:[-*•]|\d+[.)、])\s*", "", value, flags=re.M)
    value = re.sub(r"\n{3,}", "\n\n", value).strip()
    return value[:max_chars].rsplit("\n", 1)[0].strip() if len(value) > max_chars else value


def expand_short_script(
    client: AIClient,
    script: str,
    source: str,
    min_chars: int,
    max_chars: int,
) -> str:
    """Expand a short draft once without allowing facts beyond the source report."""
    if len(script) >= min_chars:
        return script
    prompt = (
        f"下面的中文播客稿只有 {len(script)} 个字符，请扩写到 {min_chars} 到 {max_chars} 个字符，"
        "目标播报时长 6 到 8 分钟。保留快速新闻扫描、2 到 3 条重点分析和结尾观察。"
        "只能使用参考材料中已有的事实；信息不足时增加解释和过渡，不得编造细节。"
        "直接输出分段的口播正文，不要 Markdown。\n\n"
        f"参考材料：\n{source}\n\n现有稿件：\n{script}"
    )
    return client.chat(
        [
            {"role": "system", "content": "你只输出事实可追溯的中文播客口播正文。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.5,
        max_tokens=3500,
    )


CHAPTER_TOPIC_RULES = (
    (r"DeepSeek.{0,24}(?:故障|宕机)|(?:故障|宕机).{0,24}DeepSeek", "DeepSeek 服务故障"),
    (r"国产大模型.{0,24}(?:数学|难题)|数学难题", "国产模型攻克数学难题"),
    (r"Apple Intelligence|苹果.{0,20}(?:AI|人工智能).{0,12}(?:入华|中国)", "苹果 AI 入华"),
    (r"推理效率|响应速度提升", "模型推理效率优化"),
    (r"RTX Spark|万卡国产算力|算力.{0,16}(?:供应|基建|焦虑)", "AI 算力与硬件"),
    (r"开源.{0,24}闭源|闭源.{0,24}开源", "开源与闭源竞争"),
    (r"(?:应用落地|商业产品化|企业级迁移)", "AI 应用商业化"),
    (r"DeepSeek.{0,20}V4", "DeepSeek V4.1 进展"),
    (r"(?:银行|金融机构).{0,30}Agent|Agent.{0,30}(?:银行|金融机构)", "Agent 金融安全"),
    (r"AI.{0,24}工作效率|(?:工程师|程序员|技术岗位|就业).{0,30}(?:效率|影响|收缩|焦虑|替代)", "AI 对就业的影响"),
    (r"(?:成本叙事|融资成本|AI股票|AI 股票|高估值|估值逻辑)", "AI 成本与估值"),
    (r"(?:技术民主化|API密钥|API 密钥|OpenCode).{0,40}(?:安全|攻击|外泄)?", "开源工具与安全风险"),
    (r"(?:地缘政治|AI基础设施|AI 基础设施).{0,30}(?:风险|建设|合作)?", "AI 基建与地缘风险"),
    (r"(?:监管|App Store).{0,30}(?:新规|审核|落地)", "AI 监管进展"),
)


def chapter_title(paragraph: str, index: int, total: int) -> str:
    if index == 0:
        return "今日速览"
    if index == total - 1:
        return "今日结语"
    for pattern, title in CHAPTER_TOPIC_RULES:
        if re.search(pattern, paragraph, flags=re.I):
            return title
    sentences = [item.strip(" *") for item in re.split(r"[。！？；;]", paragraph) if item.strip(" *")]
    generic = re.compile(r"^(?:好|好了|那么|这里|过去几年|有意思的是|当然|与此同时|说完|接下来|再来说说)")
    excerpt = next((item for item in sentences if not generic.match(item)), sentences[0] if sentences else "")
    excerpt = re.sub(
        r"^(?:重点[一二三四五六七八九十\d]+|延伸观察|首先|其次|接下来|再来看|最后)"
        r"\s*[，、,:：。]?\s*",
        "",
        excerpt,
    )
    excerpt = re.sub(r"^第[一二三四五六七八九十\d]+\s*[，、,:：。]?\s*", "", excerpt)
    title = re.split(r"[，、,:：]", excerpt, maxsplit=1)[0].strip(" *")
    if len(title) > 18:
        title = title[:18].rstrip() + "…"
    return title if len(title) >= 4 else f"重点解读 {index}"


def build_chapters(script: str) -> list[dict[str, Any]]:
    """Create readable chapter labels and duration-independent seek ratios."""
    paragraphs = [re.sub(r"\s+", " ", item).strip() for item in re.split(r"\n\s*\n", script)]
    paragraphs = [item for item in paragraphs if item]
    if not paragraphs:
        return []
    if len(paragraphs) > 6:
        chunk_size = math.ceil(len(paragraphs) / 6)
        paragraphs = [" ".join(paragraphs[index:index + chunk_size]) for index in range(0, len(paragraphs), chunk_size)]
    chapters = []
    elapsed = 0
    consumed = 0
    total_chars = sum(len(paragraph) for paragraph in paragraphs)
    for index, paragraph in enumerate(paragraphs):
        title = chapter_title(paragraph, index, len(paragraphs))
        chapters.append({
            "title": title,
            "start_seconds": elapsed,
            "start_ratio": round(consumed / total_chars, 6) if total_chars else 0,
        })
        elapsed += max(8, round(len(paragraph) / 4.2))
        consumed += len(paragraph)
    return chapters


def load_podcast_config() -> tuple[dict[str, Any], dict[str, Any]]:
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    podcast = raw.get("podcast") or {}
    normalized = load_config(str(CONFIG_PATH))
    return podcast, normalized["AI"]


def voice_label(voice: str) -> str:
    labels = {
        "zh-CN-YunyangNeural": "云扬 · 中文男声",
        "zh-CN-XiaoxiaoNeural": "晓晓 · 中文女声",
    }
    return labels.get(voice, "自然中文语音")


def cleanup_audio_history(output_root: Path, today: Date, retention_days: int) -> list[Path]:
    if retention_days <= 0 or not output_root.exists():
        return []
    cutoff = today - timedelta(days=retention_days - 1)
    removed: list[Path] = []
    for child in output_root.iterdir():
        if not child.is_dir():
            continue
        try:
            directory_date = datetime.strptime(child.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if directory_date < cutoff:
            shutil.rmtree(child)
            removed.append(child)

    latest_path = output_root / "latest.json"
    if latest_path.is_file():
        try:
            latest = json.loads(latest_path.read_text(encoding="utf-8"))
            latest_date = datetime.strptime(str(latest.get("date", "")), "%Y-%m-%d").date()
            if latest_date < cutoff:
                latest_path.unlink()
        except (json.JSONDecodeError, ValueError):
            pass
    return removed


async def synthesize(text: str, output: Path, voice: str, rate: str, volume: str) -> None:
    temporary = output.with_suffix(".tmp.mp3")
    await edge_tts.Communicate(text, voice=voice, rate=rate, volume=volume).save(str(temporary))
    temporary.replace(output)


def main() -> int:
    args = parse_args()
    podcast, ai_config = load_podcast_config()
    timezone = ZoneInfo("Asia/Shanghai")
    now = datetime.now(timezone)
    retention_days = int(podcast.get("retention_days", 90))
    removed = cleanup_audio_history(OUTPUT_ROOT, now.date(), retention_days)
    if removed:
        print(f"[podcast] removed {len(removed)} audio directorie(s) older than {retention_days} days")
    if not podcast.get("enabled", False):
        print("[podcast] disabled")
        return 0
    input_path = args.input.resolve()
    if not input_path.is_file():
        print("[podcast] latest daily report is unavailable")
        return 0

    report_html = input_path.read_text(encoding="utf-8", errors="replace")
    if args.date:
        try:
            report_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print("[podcast] --date must use YYYY-MM-DD", file=sys.stderr)
            return 2
    else:
        report_date = now.date()
    report_date_match = re.search(r"生成时间</span>\s*<span class=\"info-value\">(\d{2}-\d{2})", report_html)
    if not report_date_match or report_date_match.group(1) != report_date.strftime("%m-%d"):
        print("[podcast] report date does not match the selected date; skipped")
        return 0

    content = extract_ai_content(input_path)
    if len(content) < 80 or "AI 热点分析" not in content:
        print("[podcast] latest daily report has no completed AI analysis; skipped")
        return 0

    date = report_date.isoformat()
    source_limit = int(podcast.get("max_source_chars", 12000))
    script_minimum = int(podcast.get("min_script_chars", 1700))
    script_limit = int(podcast.get("max_script_chars", 4500))
    content = content[:source_limit]
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    voice = str(podcast.get("voice", "zh-CN-XiaoxiaoNeural"))
    rate = str(podcast.get("rate", "+0%"))
    volume = str(podcast.get("volume", "+0%"))
    output_dir = OUTPUT_ROOT / date
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = output_dir / "daily.json"
    audio_path = output_dir / "daily.mp3"
    transcript_path = output_dir / "daily.txt"
    if not args.force and metadata_path.is_file() and audio_path.is_file():
        existing = json.loads(metadata_path.read_text(encoding="utf-8"))
        if (
            existing.get("source_hash") == content_hash
            and existing.get("voice") == voice
            and existing.get("rate", "+0%") == rate
            and existing.get("volume", "+0%") == volume
        ):
            print(f"[podcast] unchanged: {audio_path}")
            return 0

    prompt_path = CONFIG_PATH.parent / str(podcast.get("prompt_file", "podcast_prompt.txt"))
    prompt = prompt_path.read_text(encoding="utf-8").replace("{content}", content)
    client = AIClient(ai_config)
    valid, error = client.validate_config()
    if not valid:
        print(f"[podcast] {error}", file=sys.stderr)
        return 1
    print("[podcast] rewriting AI analysis into a conversational script")
    script = client.chat(
        [
            {"role": "system", "content": "你只输出可直接用于中文语音合成的自然口播正文。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.7,
        max_tokens=3000,
    )
    script = clean_script(script, script_limit)
    if len(script) < script_minimum:
        print(f"[podcast] draft is short ({len(script)} chars); expanding once")
        script = expand_short_script(client, script, content, script_minimum, script_limit)
        script = clean_script(script, script_limit)
    if len(script) < 120:
        print("[podcast] generated script is unexpectedly short", file=sys.stderr)
        return 1

    print(f"[podcast] synthesizing with {voice}")
    asyncio.run(synthesize(script, audio_path, voice, rate, volume))
    transcript_path.write_text(script + "\n", encoding="utf-8")
    metadata = {
        "schema_version": 2,
        "date": date,
        "generated_at": now.isoformat(timespec="seconds"),
        "source_hash": content_hash,
        "voice": voice,
        "voice_label": voice_label(voice),
        "rate": rate,
        "volume": volume,
        "script_chars": len(script),
        "chapters": build_chapters(script),
        "format": "daily_editorial",
        "target_duration_minutes": "6-8",
        "maximum_duration_minutes": 10,
        "audio_url": f"/audio/{date}/daily.mp3",
        "transcript_url": f"/audio/{date}/daily.txt",
    }
    write_json_atomic(metadata_path, metadata)
    write_json_atomic(OUTPUT_ROOT / "latest.json", metadata)
    print(f"[podcast] generated: {audio_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
