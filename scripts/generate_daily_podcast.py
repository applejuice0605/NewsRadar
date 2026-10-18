#!/usr/bin/env python3
"""Generate one natural-language daily podcast from the latest AI report."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
import sys
import tempfile
from datetime import datetime
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


def build_chapters(script: str) -> list[dict[str, Any]]:
    """Create stable, approximate seek points from the editor-style paragraphs."""
    paragraphs = [re.sub(r"\s+", " ", item).strip() for item in re.split(r"\n\s*\n", script)]
    paragraphs = [item for item in paragraphs if item]
    if not paragraphs:
        return []
    if len(paragraphs) > 6:
        chunk_size = math.ceil(len(paragraphs) / 6)
        paragraphs = [" ".join(paragraphs[index:index + chunk_size]) for index in range(0, len(paragraphs), chunk_size)]
    chapters = []
    elapsed = 0
    labels = ["今日速览", "重点一", "重点二", "重点三", "延伸观察", "今日结语"]
    for index, paragraph in enumerate(paragraphs):
        excerpt = re.split(r"[。！？]", paragraph, maxsplit=1)[0].strip()
        title = labels[index] if index < len(labels) else f"章节 {index + 1}"
        if index > 0 and 4 <= len(excerpt) <= 18:
            title = excerpt
        chapters.append({"title": title, "start_seconds": elapsed})
        elapsed += max(8, round(len(paragraph) / 4.2))
    return chapters


def load_podcast_config() -> tuple[dict[str, Any], dict[str, Any]]:
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    podcast = raw.get("podcast") or {}
    normalized = load_config(str(CONFIG_PATH))
    return podcast, normalized["AI"]


async def synthesize(text: str, output: Path, voice: str, rate: str, volume: str) -> None:
    temporary = output.with_suffix(".tmp.mp3")
    await edge_tts.Communicate(text, voice=voice, rate=rate, volume=volume).save(str(temporary))
    temporary.replace(output)


def main() -> int:
    args = parse_args()
    podcast, ai_config = load_podcast_config()
    if not podcast.get("enabled", False):
        print("[podcast] disabled")
        return 0
    input_path = args.input.resolve()
    if not input_path.is_file():
        print("[podcast] latest daily report is unavailable")
        return 0

    report_html = input_path.read_text(encoding="utf-8", errors="replace")
    timezone = ZoneInfo("Asia/Shanghai")
    now = datetime.now(timezone)
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
    script_limit = int(podcast.get("max_script_chars", 4500))
    content = content[:source_limit]
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    output_dir = OUTPUT_ROOT / date
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = output_dir / "daily.json"
    audio_path = output_dir / "daily.mp3"
    transcript_path = output_dir / "daily.txt"
    if not args.force and metadata_path.is_file() and audio_path.is_file():
        existing = json.loads(metadata_path.read_text(encoding="utf-8"))
        if existing.get("source_hash") == content_hash:
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
    if len(script) < 120:
        print("[podcast] generated script is unexpectedly short", file=sys.stderr)
        return 1

    voice = str(podcast.get("voice", "zh-CN-YunyangNeural"))
    rate = str(podcast.get("rate", "+0%"))
    volume = str(podcast.get("volume", "+0%"))
    print(f"[podcast] synthesizing with {voice}")
    asyncio.run(synthesize(script, audio_path, voice, rate, volume))
    transcript_path.write_text(script + "\n", encoding="utf-8")
    metadata = {
        "schema_version": 2,
        "date": date,
        "generated_at": now.isoformat(timespec="seconds"),
        "source_hash": content_hash,
        "voice": voice,
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
