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
import subprocess
import sys
import tempfile
from datetime import date as Date
from datetime import datetime, timedelta
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

import edge_tts
import yaml

from trendradar.ai.client import AIClient
from trendradar.core.loader import load_config


PROJECT_DIR = Path(__file__).resolve().parents[1] / "TrendRadar"
CONFIG_PATH = PROJECT_DIR / "config" / "config.yaml"
LATEST_DAILY = PROJECT_DIR / "output" / "html" / "latest" / "daily.html"
OUTPUT_ROOT = PROJECT_DIR / "output" / "audio"
CITATION_PATTERN = r"(?<![A-Za-z0-9_])N[0-9]{3,}(?![A-Za-z0-9_])"


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
    parser.add_argument("--fact-pack", type=Path, help="canonical v1 fact pack JSON (takes precedence over HTML)")
    parser.add_argument("--output-root", type=Path, default=OUTPUT_ROOT, help="isolated audio output directory")
    return parser.parse_args()


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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
    """Compatibility shim: sparse news must never trigger model expansion."""
    return script


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


def build_chapters(script: str, duration_seconds: float | None = None) -> list[dict[str, Any]]:
    """Character ratios approximate chapter positions within measured audio."""
    paragraphs = [re.sub(r"\s+", " ", item).strip() for item in re.split(r"\n\s*\n", script)]
    paragraphs = [item for item in paragraphs if item]
    if not paragraphs:
        return []
    if len(paragraphs) > 6:
        chunk_size = math.ceil(len(paragraphs) / 6)
        paragraphs = [" ".join(paragraphs[index:index + chunk_size]) for index in range(0, len(paragraphs), chunk_size)]
    chapters = []
    consumed = 0
    total_chars = sum(len(paragraph) for paragraph in paragraphs)
    for index, paragraph in enumerate(paragraphs):
        title = chapter_title(paragraph, index, len(paragraphs))
        if duration_seconds is not None:
            topic = re.sub(r"^据[^：]+：", "", paragraph)
            title = re.split(r"[。！？；\n]", topic, maxsplit=1)[0][:24]
        chapters.append({
            "title": title,
            "start_seconds": round(consumed / total_chars * duration_seconds, 3) if duration_seconds else 0,
            "start_ratio": round(consumed / total_chars, 6) if total_chars else 0,
            "timing_approximate": True,
        })
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
        if child.is_symlink() or not child.is_dir():
            continue
        try:
            directory_date = datetime.strptime(child.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if directory_date.isoformat() != child.name:
            continue
        if directory_date < cutoff:
            shutil.rmtree(child)
            removed.append(child)

    latest_path = output_root / "latest.json"
    if not latest_path.is_symlink() and latest_path.is_file():
        try:
            latest = json.loads(latest_path.read_text(encoding="utf-8"))
            latest_date = datetime.strptime(str(latest.get("date", "")), "%Y-%m-%d").date()
            if latest_date < cutoff:
                latest_path.unlink()
        except (json.JSONDecodeError, ValueError):
            pass
    return removed


class FactPackParser(HTMLParser):
    """Read raw JSON script data without interpreting HTML or executing scripts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.active = False
        self.blocks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script" and values.get("id") == "newsradar-fact-pack":
            if values.get("type") != "application/json":
                raise ValueError("fact pack script must use application/json")
            self.active = True
            self.blocks.append("")

    def handle_data(self, data: str) -> None:
        if self.active:
            self.blocks[-1] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self.active = False


def embedded_fact_pack(html: str) -> dict[str, Any] | None:
    parser = FactPackParser()
    parser.feed(html)
    if not parser.blocks:
        return None
    if len(parser.blocks) != 1 or parser.active:
        raise ValueError("expected one complete embedded fact pack")
    pack = json.loads(parser.blocks[0])
    if not isinstance(pack, dict):
        raise ValueError("embedded fact pack must be an object")
    return pack


def safe_url(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    decoded = unquote(value)
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in decoded) or any(c in decoded for c in "\\<>\"'`"):
        return False
    try:
        url = urlsplit(value)
        return bool(url.scheme in {"https", "http"} and url.hostname
                    and url.username is None and url.password is None and url.port != 0)
    except ValueError:
        return False


def validate_fact_pack(pack: Any, report_date: Date) -> dict[str, Any]:
    """Verify provenance/extractive quotes, not the truth of source statements."""
    if not isinstance(pack, dict) or type(pack.get("schema_version")) is not int or pack["schema_version"] != 1:
        raise ValueError("expected fact pack schema_version 1")
    if pack.get("date") != report_date.isoformat():
        raise ValueError("fact pack date does not match selected date")
    try:
        stamp = datetime.fromisoformat(pack["generated_at"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("generated_at needs timezone")
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        raise ValueError("fact pack generated_at must be an ISO timestamp with timezone") from exc
    articles, claims, sources = (pack.get(key) for key in ("articles", "claims", "evidence_sources"))
    if not isinstance(articles, list) or not isinstance(claims, list) or not isinstance(sources, dict):
        raise ValueError("fact pack requires articles, claims and evidence_sources")

    def record(value: Any) -> None:
        if not isinstance(value, dict) or not isinstance(value.get("title"), str):
            raise ValueError("source record requires a text title")
        if not value["title"].strip() and not value.get("summary"):
            raise ValueError("source record requires a title or summary")
        if not safe_url(value.get("url")):
            raise ValueError("source record requires a safe HTTP(S) URL")
        for key in ("source", "summary"):
            if not isinstance(value.get(key, ""), str):
                raise ValueError(f"source {key} must be text")

    for source_id, source in sources.items():
        # Article-only canonical packs may key this catalog by stable article ID.
        if not isinstance(source_id, str) or not source_id.strip() or any(ord(c) < 32 for c in source_id):
            raise ValueError("invalid evidence source ID")
        record(source)
    article_ids = set()
    for article in articles:
        record(article)
        if not article["title"].strip():
            raise ValueError("article requires a title")
        article_id = article.get("id")
        if not isinstance(article_id, str) or not article_id or article_id in article_ids:
            raise ValueError("articles require unique IDs")
        article_ids.add(article_id)

    # Validate nested material links as well; no link is fetched by this script.
    def links(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if (key in {"url", "href"} or key.endswith("_url")) and item not in (None, ""):
                    if not safe_url(item):
                        raise ValueError("unsafe URL in fact pack")
                else:
                    links(item)
        elif isinstance(value, list):
            for item in value:
                links(item)
    links(pack)
    for article in articles:
        materials = article.get("primary_materials", [])
        if isinstance(materials, list):
            for material in materials:
                if isinstance(material, str) and not safe_url(material):
                    raise ValueError("primary material link must be a safe HTTP(S) URL")
    sections = {"core_trends", "sentiment_controversy", "signals", "rss_insights", "outlook_strategy"}
    for claim in claims:
        if not isinstance(claim, dict):
            raise ValueError("claim must be an object")
        section, fact, ids = (claim.get(key) for key in ("section", "fact", "source_ids"))
        if not isinstance(section, str) or section not in sections:
            raise ValueError("unknown claim section")
        if not isinstance(fact, str) or not fact.strip() or not isinstance(ids, list) or not ids:
            raise ValueError("claim requires fact and source_ids")
        for source_id in ids:
            if not isinstance(source_id, str) or source_id not in sources:
                raise ValueError("unknown claim citation ID")
            source = sources[source_id]
            if not any(fact.strip() in source.get(key, "") for key in ("title", "summary")):
                raise ValueError("claim fact is not a quote from its cited source record")
        for key in ("interpretation", "uncertainty"):
            if not isinstance(claim.get(key, ""), str):
                raise ValueError(f"claim {key} must be text")
        for source_id in re.findall(CITATION_PATTERN, " ".join(str(claim.get(k, "")) for k in ("fact", "interpretation", "uncertainty"))):
            if source_id not in sources:
                raise ValueError("unknown citation in claim text")
    if not claims and not 3 <= len(articles) <= 5:
        raise ValueError("article-only fact packs require 3 to 5 articles")
    return pack


def spoken_text(value: str) -> str:
    """Remove links and markup, never turn external text into instructions."""
    value = unescape(value)
    value = re.sub(r"\[([^]\n]+)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"<[^>]*>", "", value)
    value = re.sub(r"(?:https?://|www\.)[^\s，。！？；、<>]+", "", value, flags=re.I)
    value = re.sub(r"\[?" + CITATION_PATTERN + r"\]?", "", value)
    return re.sub(r"\s+", " ", value).strip()


ARTICLE_ONLY_REASON = "事实包仅含原始新闻，缺少通过来源校验的分析；本期仅播报来源陈述，不生成延伸判断。"


def script_from_fact_pack(pack: dict[str, Any]) -> tuple[str, str, str]:
    """Deterministic narration: no model can add facts or stretch a sparse day."""
    parts = []
    if pack["claims"]:
        for claim in pack["claims"]:
            names = list(dict.fromkeys(spoken_text(pack["evidence_sources"][i].get("source", ""))
                                       or "所引新闻来源" for i in claim["source_ids"]))
            paragraph = f"据{'、'.join(names)}的来源陈述：{spoken_text(claim['fact'])}"
            if claim.get("interpretation", "").strip():
                paragraph += "。条件性分析，尚待验证：" + spoken_text(claim["interpretation"])
            paragraph += "。不确定性：" + spoken_text(claim.get("uncertainty", "") or "仅依据输入标题或摘要，未独立核验。")
            parts.append(paragraph)
        return "\n\n".join(parts), "fact_pack_claims", ""
    for article in pack["articles"]:
        paragraph = f"据{spoken_text(article.get('source') or article.get('source_name', '')) or '所引新闻来源'}报道：{spoken_text(article['title'])}"
        if article.get("summary", "").strip():
            paragraph += "。来源摘要：" + spoken_text(article["summary"])
        parts.append(paragraph)
    return "\n\n".join(parts), "fact_pack_articles", ARTICLE_ONLY_REASON


def cache_hash(prompt: str, content: str, voice: str, rate: str, volume: str, script_limit: int) -> str:
    payload = ["podcast-v4", prompt, content, voice, rate, volume, script_limit]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode("utf-8")).hexdigest()


def measure_duration(path: Path) -> float:
    """Use actual MP3 duration. Missing/broken probes never imply a safe length."""
    probe = shutil.which("ffprobe")
    if probe:
        try:
            result = subprocess.run(
                [probe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                check=True, capture_output=True, text=True, timeout=30,
            )
            duration = float(json.loads(result.stdout)["format"]["duration"])
            if math.isfinite(duration) and duration > 0:
                return duration
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            pass
    try:
        from mutagen.mp3 import MP3
        duration = float(MP3(path).info.length)
        if math.isfinite(duration) and duration > 0:
            return duration
    except Exception:
        pass
    raise ValueError("cannot measure audio duration; nothing published (install ffprobe or mutagen)")


def validate_duration(duration: float) -> None:
    if not math.isfinite(duration) or not 0 < duration <= 600:
        raise ValueError("audio duration must be measured and no more than 600 seconds")


async def synthesize(text: str, output: Path, voice: str, rate: str, volume: str) -> None:
    # Only a private staging path is passed here. Even a partial save stays private.
    await edge_tts.Communicate(text, voice=voice, rate=rate, volume=volume).save(str(output))


def publish_bundle(stage: Path, output_root: Path, metadata: dict[str, Any]) -> None:
    """Immutable URLs keep readers on a complete generation across index updates.

    latest.json advances only to an equal or newer report date. The per-date
    index always points at a complete bundle, including historical rebuilds,
    and is rolled back if reading or committing latest fails.
    Never overwrite a previous audio/transcript, including old daily.mp3 URLs.
    """
    day = output_root / metadata["date"]
    if day.is_symlink():
        raise ValueError("refusing to publish through a date directory symlink")
    day.mkdir(parents=True, exist_ok=True)
    generation = uuid4().hex
    metadata["audio_url"] = f"/audio/{metadata['date']}/{generation}/daily.mp3"
    metadata["transcript_url"] = f"/audio/{metadata['date']}/{generation}/daily.txt"
    if (stage / "facts.json").is_file():
        metadata["fact_pack_url"] = f"/audio/{metadata['date']}/{generation}/facts.json"
    write_json_atomic(stage / "daily.json", metadata)
    index = day / "daily.json"
    previous = index.read_bytes() if index.exists() else None
    bundle = day / generation
    stage.replace(bundle)
    try:
        write_json_atomic(index, metadata)
        latest_path = output_root / "latest.json"
        latest_date = None
        try:
            current = json.loads(latest_path.read_text(encoding="utf-8"))
            candidate = Date.fromisoformat(current["date"])
            if candidate.isoformat() == current["date"]:
                latest_date = candidate
        except (FileNotFoundError, ValueError, TypeError, KeyError):
            # A missing/malformed index has no valid date to preserve. Other
            # I/O failures still roll back rather than risk losing a newer one.
            pass
        if latest_date is None or Date.fromisoformat(metadata["date"]) >= latest_date:
            write_json_atomic(latest_path, metadata)
    except Exception:
        if previous is None:
            index.unlink(missing_ok=True)
        else:
            # Restore the previous date index without a partial JSON write.
            with tempfile.NamedTemporaryFile(dir=day, delete=False) as handle:
                handle.write(previous)
                restore = Path(handle.name)
            restore.replace(index)
        # Keep this complete generation for readers that observed the date index
        # before rollback. Normal 90-day cleanup removes it with its date.
        raise


def main() -> int:
    args = parse_args()
    try:
        return generate(args)
    except Exception as exc:
        # AI/TTS libraries have provider-specific exception types. Staging still
        # cleans up on every failure and no unknown-length audio is published.
        print(f"[podcast] {exc}", file=sys.stderr)
        return 1


def generate(args: argparse.Namespace) -> int:
    podcast, ai_config = load_podcast_config()
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    if not podcast.get("enabled", False):
        print("[podcast] disabled")
        return 0
    output_root = args.output_root.resolve()
    report_date = Date.fromisoformat(args.date) if args.date else now.date()
    if args.date and report_date.isoformat() != args.date:
        raise ValueError("--date must use YYYY-MM-DD")
    pack = None
    html = ""
    if args.fact_pack:
        pack = json.loads(args.fact_pack.read_text(encoding="utf-8"))
    elif args.input.is_file():
        html = args.input.read_text(encoding="utf-8", errors="replace")
        pack = embedded_fact_pack(html)
    else:
        print("[podcast] latest daily report is unavailable")
        return 0

    source_kind, reason = "legacy_unverified", "旧版 HTML 缺少结构化事实包，口播未经 claim 来源校验。"
    fact_pack_hash = None
    if pack is not None:
        validate_fact_pack(pack, report_date)
        serialized_pack = json.dumps(pack, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fact_pack_hash = hashlib.sha256(serialized_pack.encode("utf-8")).hexdigest()
        script, source_kind, reason = script_from_fact_pack(pack)
        # Deterministic narration is the projection of semantic fields we
        # actually speak (facts, interpretations, uncertainty, source names,
        # or article titles/summaries). Do not invalidate playback/resume for
        # generated_at, fetch timestamps, enrichment status or unused records.
        content = json.dumps({"source_kind": source_kind, "script": script},
                             ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    else:
        # Compatibility applies solely to the existing production HTML route.
        # A custom/historical HTML input must contain a canonical fact pack.
        if args.fact_pack or args.input.resolve() != LATEST_DAILY.resolve() or args.date:
            raise ValueError("custom or historical HTML requires a structured fact pack")
        match = re.search(r'生成时间</span>\s*<span class="info-value">(\d{2}-\d{2})', html)
        if not match or match.group(1) != report_date.strftime("%m-%d"):
            print("[podcast] report date mismatch; skipped")
            return 0
        content = extract_ai_content(args.input)
        if len(content) < 80 or "AI 热点分析" not in content:
            print("[podcast] no completed AI analysis; skipped")
            return 0
        content = content[:int(podcast.get("max_source_chars", 12000))]

    voice = str(podcast.get("voice", "zh-CN-YunyangNeural"))
    rate, volume = str(podcast.get("rate", "+0%")), str(podcast.get("volume", "+0%"))
    script_limit = int(podcast.get("max_script_chars", 4500))
    prompt_path = CONFIG_PATH.parent / str(podcast.get("prompt_file", "podcast_prompt.txt"))
    prompt_template = prompt_path.read_text(encoding="utf-8")
    digest = cache_hash(prompt_template, content, voice, rate, volume, script_limit)
    metadata_path = output_root / report_date.isoformat() / "daily.json"
    if not args.force and metadata_path.is_file():
        try:
            existing = json.loads(metadata_path.read_text(encoding="utf-8"))
            relative = Path(existing["audio_url"].removeprefix("/audio/"))
            cached_audio = output_root / relative
            cached_transcript = cached_audio.with_suffix(".txt")
            cached_metadata = cached_audio.with_suffix(".json")
            if (existing.get("cache_hash") == digest and not relative.is_absolute()
                    and cached_audio.resolve().is_relative_to(output_root) and cached_audio.is_file()
                    and cached_transcript.is_file() and cached_metadata.is_file()):
                validate_duration(measure_duration(cached_audio))
                print(f"[podcast] unchanged: {cached_audio}")
                return 0
        except (ValueError, KeyError, TypeError):
            pass
    if pack is None:
        client = AIClient(ai_config)
        valid, error = client.validate_config()
        if not valid:
            raise ValueError(error)
        script = client.chat([
            {"role": "system", "content": "你只输出来源材料支持的中文口播正文；事实与推测必须区分。"},
            {"role": "user", "content": prompt_template.replace("{content}", content)},
        ], temperature=0.5, max_tokens=3000)
        script = clean_script(spoken_text_blocks(script), script_limit)
        if len(script) < 40:
            raise ValueError("legacy script has no usable brief (under 40 characters)")
    # Never truncate a structured fact or manufacture filler to meet a minimum.
    if not script.strip():
        raise ValueError("no speakable source material")
    output_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".podcast-", dir=output_root) as temporary:
        stage = Path(temporary) / "bundle"
        stage.mkdir()
        audio = stage / "daily.mp3"
        print(f"[podcast] synthesizing {source_kind} with {voice}")
        asyncio.run(synthesize(script, audio, voice, rate, volume))
        if not audio.is_file() or audio.stat().st_size == 0:
            raise ValueError("TTS produced no audio")
        duration = measure_duration(audio)
        validate_duration(duration)
        (stage / "daily.txt").write_text(script + "\n", encoding="utf-8")
        metadata = {
            "schema_version": 3, "date": report_date.isoformat(),
            "preview_only": output_root != OUTPUT_ROOT.resolve(),
            "generated_at": now.isoformat(timespec="seconds"),
            "source_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "fact_pack_hash": fact_pack_hash,
            "cache_hash": digest, "source_kind": source_kind, "source_reason": reason,
            "analysis_available": bool(pack and pack["claims"]),
            "claim_grounded": source_kind == "fact_pack_claims",
            "analysis_unavailable_reason": reason,
            "voice": voice, "voice_label": voice_label(voice), "rate": rate, "volume": volume,
            "duration_seconds": duration, "script_chars": len(script),
            "chapters": build_chapters(script, duration),
            "format": "daily_editorial", "maximum_duration_minutes": 10,
        }
        if pack is not None:
            write_json_atomic(stage / "facts.json", pack)
        publish_bundle(stage, output_root, metadata)
    # Failed input/TTS/duration/publication must not delete a previous episode.
    if report_date == now.date():
        cleanup_audio_history(output_root, now.date(), int(podcast.get("retention_days", 90)))
    print(f"[podcast] generated {metadata['audio_url']} ({duration:.1f}s)")
    return 0


def spoken_text_blocks(script: str) -> str:
    return "\n\n".join(spoken_text(part) for part in re.split(r"\n\s*\n", script))


if __name__ == "__main__":
    raise SystemExit(main())
