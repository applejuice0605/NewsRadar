#!/usr/bin/env python3
"""Generate public health and trend pages from TrendRadar's local output."""

from __future__ import annotations

import argparse
import html
import importlib.util
import json
import math
import os
import re
import sqlite3
import tempfile
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo


REPO_DIR = Path(__file__).resolve().parents[1]
_quality_spec = importlib.util.spec_from_file_location("news_quality", REPO_DIR / "TrendRadar/trendradar/ai/news_quality.py")
_quality_module = importlib.util.module_from_spec(_quality_spec)
_quality_spec.loader.exec_module(_quality_module)
peripheral_reason = _quality_module.peripheral_reason
DEFAULT_SITE_DIR = REPO_DIR / "docs"
RANGES = (1, 7, 30, 90)
TOP_ARTICLES_LIMIT = 8


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_time(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    try:
        datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        return None
    return candidate


def _number(value: Any, *, integer: bool = False, minimum: float = 0) -> int | float | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value) if integer else float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(float(result)) or result < minimum:
        return None
    return result


def _boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1", "success", "ok"}:
            return True
        if lowered in {"false", "no", "0", "failed", "error"}:
            return False
    return None


def _status(value: Any, default: str = "unknown") -> str:
    if not isinstance(value, str):
        return default
    normalized = value.strip().lower().replace(" ", "_")
    aliases = {
        "ok": "success",
        "healthy": "success",
        "passed": "success",
        "error": "failed",
        "failure": "failed",
        "warn": "warning",
        "degraded": "warning",
        "pending": "pending",
        "skipped": "skipped",
        "disabled": "disabled",
        "unknown": "unknown",
    }
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in {"success", "failed", "warning", "pending", "skipped", "disabled", "unknown"} else default


_SENSITIVE_TEXT = re.compile(
    r"(?:https?://|webhook|token|secret|api[_-]?key|password|"
    r"(?:^|\s)/(?:Users|home|var|private|tmp|opt|etc)/|"
    r"[A-Za-z]:\\|\b[A-Z][A-Z0-9]{2,}(?:_[A-Z0-9]+)+\b)",
    re.IGNORECASE,
)


def _public_reason(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.strip().split())
    if not text:
        return None
    if len(text) > 240 or _SENSITIVE_TEXT.search(text):
        return "details_redacted"
    return text


def _commit(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if re.fullmatch(r"[0-9a-fA-F]{7,40}", candidate) else None


def _public_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    if not candidate or len(candidate) > 2048:
        return ""
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return ""
    return candidate if parsed.scheme.lower() in {"http", "https"} and parsed.netloc else ""


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name)
    return value if isinstance(value, dict) else {}


def _first(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def build_public_health(raw: dict[str, Any] | None, now: datetime | None = None) -> dict[str, Any]:
    """Return a strict, public-safe projection of runtime health data."""
    now = (now or _utc_now()).astimezone(timezone.utc)
    raw = raw if isinstance(raw, dict) else {}
    collection_raw = _section(raw, "collection")
    notification_raw = _section(raw, "notification")
    publish_raw = _section(raw, "publish")
    schedule_raw = _section(raw, "schedule")

    last_completed = _iso_time(_first(collection_raw, "last_completed_at", "completed_at", "finished_at"))
    interval = _number(_first(schedule_raw, "interval_minutes", "interval"), integer=True, minimum=1)
    interval = interval if interval is not None else 60
    next_expected = _iso_time(_first(schedule_raw, "next_expected_at", "next_run_at"))
    completed_dt = _parse_datetime(last_completed)
    if next_expected is None and completed_dt is not None:
        next_expected = (completed_dt + timedelta(minutes=int(interval))).isoformat()

    gap_minutes = _number(_first(collection_raw, "gap_minutes", "minutes_since_completed"))
    if gap_minutes is None and completed_dt is not None:
        gap_minutes = max(0.0, (now - completed_dt).total_seconds() / 60)

    success = _boolean(_first(collection_raw, "success", "ok"))
    source_failed = _number(_first(collection_raw, "source_failed", "sources_failed"), integer=True)
    notification_status = _status(_first(notification_raw, "status", "result"))
    publish_status = _status(_first(publish_raw, "status", "result"))
    overall = _status(raw.get("overall_status"))
    if overall == "success":
        overall_status = "healthy"
    elif overall == "failed":
        overall_status = "down"
    elif overall == "warning":
        overall_status = "degraded"
    elif overall != "unknown":
        overall_status = overall
    elif (
        success is False
        or "failed" in {notification_status, publish_status}
        or bool(source_failed)
        or (completed_dt is not None and (now - completed_dt).total_seconds() > (int(interval) + 15) * 60)
    ):
        overall_status = "degraded"
    elif success is True and notification_status != "failed" and publish_status != "failed":
        overall_status = "healthy"
    else:
        overall_status = "unknown"

    return {
        "generated_at": now.isoformat(),
        "podcast": _section(raw, "podcast"),
        "overall_status": overall_status,
        "collection": {
            "last_started_at": _iso_time(_first(collection_raw, "last_started_at", "started_at")),
            "last_completed_at": last_completed,
            "duration_seconds": _number(_first(collection_raw, "duration_seconds", "duration")),
            "success": success,
            "source_success": _number(_first(collection_raw, "source_success", "sources_succeeded"), integer=True),
            "source_failed": source_failed,
            "item_count": _number(_first(collection_raw, "item_count", "items"), integer=True),
            "gap_minutes": round(float(gap_minutes), 1) if gap_minutes is not None else None,
            "catch_up": _boolean(_first(collection_raw, "catch_up", "is_catch_up")),
        },
        "notification": {
            "last_attempt_at": _iso_time(_first(notification_raw, "last_attempt_at", "attempted_at")),
            "last_success_at": _iso_time(_first(notification_raw, "last_success_at", "succeeded_at")),
            "status": notification_status,
            "reason": _public_reason(_first(notification_raw, "reason", "message")),
        },
        "publish": {
            "last_attempt_at": _iso_time(_first(publish_raw, "last_attempt_at", "attempted_at")),
            "last_success_at": _iso_time(_first(publish_raw, "last_success_at", "succeeded_at")),
            "status": publish_status,
            "commit": _commit(_first(publish_raw, "commit", "commit_sha", "revision")),
        },
        "schedule": {
            "interval_minutes": interval,
            "next_expected_at": next_expected,
        },
    }


def build_public_pilot(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Project the isolated source-pilot result without exposing endpoint details."""
    raw = raw if isinstance(raw, dict) else {}
    configured = _boolean(raw.get("configured")) is True
    recommendations = raw.get("recommendations") if isinstance(raw.get("recommendations"), dict) else {}
    sources = []
    for source in raw.get("sources", []) if isinstance(raw.get("sources"), list) else []:
        if not isinstance(source, dict):
            continue
        source_id = source.get("source")
        if not isinstance(source_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", source_id):
            continue
        recommendation = recommendations.get(source_id) if isinstance(recommendations.get(source_id), dict) else {}
        metrics = recommendation.get("metrics") if isinstance(recommendation.get("metrics"), dict) else {}
        sources.append(
            {
                "source": source_id,
                "success": _boolean(source.get("success")),
                "adapter": source.get("adapter") if source.get("adapter") in {"newsnow", "feed"} else "unknown",
                "error": source.get("error") if source.get("error") in {"http_error", "network_error", "invalid_json", "invalid_payload", "invalid_status", "invalid_items", "request_error", "invalid_feed", "empty_feed"} else None,
                "http_status": _number(source.get("http_status"), integer=True),
                "item_count": _number(source.get("item_count"), integer=True),
                "interest_hit_rate": _number(source.get("interest_hit_rate")),
                "duplicate_rate": _number(source.get("duplicate_rate")),
                "sample_days": _number(metrics.get("sample_days"), integer=True),
                "recommendation": (
                    recommendation.get("status")
                    if recommendation.get("status") in {"observing", "hold", "promote"}
                    else "unknown"
                )
                if recommendation
                else ("not_configured" if not configured else "observing"),
            }
        )

    recommendation_states = {source["recommendation"] for source in sources}
    if not configured:
        status = "not_configured"
    elif recommendation_states and recommendation_states == {"promote"}:
        status = "ready"
    elif "hold" in recommendation_states:
        status = "hold"
    else:
        status = "observing"
    return {
        "configured": configured,
        "generated_at": _iso_time(raw.get("generated_at")),
        "status": status,
        "sources": sources,
    }


def build_public_source_feed(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Expose candidate headlines without leaking adapter endpoints or local paths."""
    raw = raw if isinstance(raw, dict) else {}
    groups = []
    for source in raw.get("sources", []) if isinstance(raw.get("sources"), list) else []:
        if not isinstance(source, dict) or source.get("success") is not True:
            continue
        source_id = source.get("source")
        if not isinstance(source_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", source_id):
            continue
        items = []
        for item in source.get("items", []) if isinstance(source.get("items"), list) else []:
            if not isinstance(item, dict):
                continue
            title = item.get("title")
            if not isinstance(title, str) or not title.strip():
                continue
            tags = [
                tag for tag in item.get("interest_rules", [])
                if isinstance(tag, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", tag)
            ] if isinstance(item.get("interest_rules"), list) else []
            items.append({
                "title": title.strip()[:300],
                "url": _public_url(item.get("url")),
                "interest_rules": tags[:5],
                "duplicate_in_official_news": _boolean(item.get("duplicate_in_official_news")) is True,
            })
        groups.append({"source": source_id, "items": items[:50]})
    return {
        "generated_at": _iso_time(raw.get("generated_at")),
        "observation_only": True,
        "sources": groups,
    }


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _db_date(path: Path) -> date | None:
    try:
        return date.fromisoformat(path.stem)
    except ValueError:
        return None


def _connect_readonly(path: Path) -> sqlite3.Connection:
    uri = "file:" + quote(str(path.resolve()), safe="/") + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    return connection


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _rank_details(connection: sqlite3.Connection) -> dict[int, tuple[int | None, float]]:
    histories: dict[int, list[tuple[str, int, int]]] = defaultdict(list)
    for row in connection.execute(
        "SELECT id, news_item_id, rank, crawl_time FROM rank_history ORDER BY news_item_id, crawl_time, id"
    ):
        rank = int(row[2])
        if rank > 0:
            histories[int(row[1])].append((str(row[3] or ""), int(row[0]), rank))
    result: dict[int, tuple[int | None, float]] = {}
    for item_id, samples in histories.items():
        ranks = [sample[2] for sample in samples]
        first_rank, last_rank = ranks[0], ranks[-1]
        momentum = (first_rank - last_rank) / max(first_rank, last_rank, 1) * 100
        result[item_id] = (min(ranks), round(momentum, 1))
    return result


def _ai_tags(connection: sqlite3.Connection, source_type: str) -> dict[int, list[tuple[str, float]]]:
    rows = connection.execute(
        """
        SELECT r.news_item_id, t.tag, r.relevance_score
        FROM ai_filter_results AS r
        JOIN ai_filter_tags AS t ON t.id = r.tag_id
        WHERE r.source_type = ? AND r.status = 'active' AND t.status = 'active'
        ORDER BY t.priority, t.id, r.relevance_score DESC
        """,
        (source_type,),
    )
    tags: dict[int, list[tuple[str, float]]] = defaultdict(list)
    for row in rows:
        tag = str(row[1] or "").strip()
        if tag:
            tags[int(row[0])].append((tag, float(row[2] or 0)))
    return tags


def _load_news_database(path: Path, day: date) -> list[dict[str, Any]]:
    with closing(_connect_readonly(path)) as connection:
        tables = _tables(connection)
        required = {"news_items", "rank_history"}
        if not required.issubset(tables):
            raise sqlite3.DatabaseError("missing news tables")
        ranks = _rank_details(connection)
        tags = (
            _ai_tags(connection, "hotlist")
            if {"ai_filter_results", "ai_filter_tags"}.issubset(tables)
            else {}
        )
        has_platforms = "platforms" in tables
        news_columns = _columns(connection, "news_items")
        source_join = "LEFT JOIN platforms AS p ON p.id = n.platform_id" if has_platforms else ""
        source_name = "COALESCE(p.name, n.platform_id)" if has_platforms else "n.platform_id"
        first_seen = "n.first_crawl_time" if "first_crawl_time" in news_columns else "NULL"
        last_seen = "n.last_crawl_time" if "last_crawl_time" in news_columns else "NULL"
        crawl_count = "n.crawl_count" if "crawl_count" in news_columns else "1"
        rows = connection.execute(
            f"""
            SELECT n.id, n.title, n.platform_id, {source_name} AS source_name,
                   n.url, n.mobile_url, n.rank, {first_seen}, {last_seen}, {crawl_count}
            FROM news_items AS n {source_join}
            """
        )
        records = []
        for row in rows:
            item_id = int(row[0])
            best_rank, momentum = ranks.get(item_id, (int(row[6]) if int(row[6]) > 0 else None, 0.0))
            records.append(
                {
                    "key": f"{day.isoformat()}:news:{item_id}",
                    "date": day,
                    "title": str(row[1]),
                    "source_id": str(row[2]),
                    "source": str(row[3] or row[2]),
                    "url": _public_url(row[4] or row[5]),
                    "best_rank": best_rank,
                    "momentum": momentum,
                    "tags": tags.get(item_id, []),
                    "published_at": None,
                    "first_seen_at": _iso_time(row[7]),
                    "last_seen_at": _iso_time(row[8]),
                    "crawl_count": int(row[9] or 1),
                    "summary": None,
                }
            )
        return records


def _load_rss_database(path: Path, day: date) -> list[dict[str, Any]]:
    with closing(_connect_readonly(path)) as connection:
        tables = _tables(connection)
        if "rss_items" not in tables:
            raise sqlite3.DatabaseError("missing rss_items")
        tags = (
            _ai_tags(connection, "rss")
            if {"ai_filter_results", "ai_filter_tags"}.issubset(tables)
            else {}
        )
        has_feeds = "rss_feeds" in tables
        rss_columns = _columns(connection, "rss_items")
        source_join = "LEFT JOIN rss_feeds AS f ON f.id = i.feed_id" if has_feeds else ""
        source_name = "COALESCE(f.name, i.feed_id)" if has_feeds else "i.feed_id"
        published_at = "i.published_at" if "published_at" in rss_columns else "NULL"
        first_seen = "i.first_crawl_time" if "first_crawl_time" in rss_columns else "NULL"
        last_seen = "i.last_crawl_time" if "last_crawl_time" in rss_columns else "NULL"
        crawl_count = "i.crawl_count" if "crawl_count" in rss_columns else "1"
        summary = "i.summary" if "summary" in rss_columns else "NULL"
        rows = connection.execute(
            f"""
            SELECT i.id, i.title, i.feed_id, {source_name} AS source_name, i.url,
                   {published_at}, {first_seen}, {last_seen}, {crawl_count}, {summary}
            FROM rss_items AS i {source_join}
            """
        )
        return [
            {
                "key": f"{day.isoformat()}:rss:{int(row[0])}",
                "date": day,
                "title": str(row[1]),
                "source_id": f"rss:{row[2]}",
                "source": str(row[3] or row[2]),
                "url": _public_url(row[4]),
                "best_rank": None,
                "momentum": 0.0,
                "tags": tags.get(int(row[0]), []),
                "published_at": _iso_time(row[5]),
                "first_seen_at": _iso_time(row[6]),
                "last_seen_at": _iso_time(row[7]),
                "crawl_count": int(row[8] or 1),
                "summary": str(row[9]).strip()[:1200] if row[9] else None,
            }
            for row in rows
        ]


def load_articles(output_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Load databases independently; one corrupt database never stops the build."""
    articles: list[dict[str, Any]] = []
    skipped: list[str] = []
    for kind, loader in (("news", _load_news_database), ("rss", _load_rss_database)):
        directory = output_dir / kind
        for path in sorted(directory.glob("*.db")) if directory.exists() else []:
            day = _db_date(path)
            if day is None:
                continue
            try:
                articles.extend(loader(path, day))
            except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
                skipped.append(f"{path.name}: {type(exc).__name__}")
    return articles, skipped


def _article_summary(article: dict[str, Any], relevance: float | None) -> dict[str, Any]:
    return {
        "title": article["title"],
        "url": article["url"],
        "source": article["source"],
        "date": article["date"].isoformat(),
        "relevance": round(relevance, 3) if relevance is not None else None,
        "best_rank": article["best_rank"],
        "momentum": article["momentum"],
    }


def _aggregate_range(articles: list[dict[str, Any]], days: int, as_of: date) -> dict[str, Any]:
    start = as_of - timedelta(days=days - 1)
    selected = [article for article in articles if start <= article["date"] <= as_of]
    covered_days = len({article["date"] for article in selected})
    has_ai = any(article["tags"] for article in selected)
    basis = "ai_filter" if has_ai else "platform_fallback"
    buckets: dict[str, list[tuple[dict[str, Any], float | None]]] = defaultdict(list)
    for article in selected:
        if has_ai:
            for tag, relevance in article["tags"]:
                buckets[tag].append((article, relevance))
        else:
            buckets[article["source"]].append((article, None))

    topics = []
    for tag, entries in buckets.items():
        sources = {article["source_id"] for article, _ in entries}
        relevance_values = [score for _, score in entries if score is not None]
        ranks = [article["best_rank"] for article, _ in entries if article["best_rank"] is not None]
        momentum = sum(float(article["momentum"]) for article, _ in entries) / len(entries)
        ordered = sorted(
            entries,
            key=lambda item: (
                -(item[1] if item[1] is not None else -1),
                item[0]["best_rank"] if item[0]["best_rank"] is not None else 10**9,
                -item[0]["date"].toordinal(),
                item[0]["title"],
            ),
        )
        topics.append(
            {
                "tag": tag,
                "basis": basis,
                "article_count": len(entries),
                "source_count": len(sources),
                "average_relevance": round(sum(relevance_values) / len(relevance_values), 3) if relevance_values else None,
                "best_rank": min(ranks) if ranks else None,
                "momentum": round(momentum, 1),
                "top_articles": [_article_summary(article, score) for article, score in ordered[:TOP_ARTICLES_LIMIT]],
            }
        )
    topics.sort(key=lambda topic: (-topic["article_count"], -(topic["average_relevance"] or 0), topic["tag"]))
    visible_sources = {
        article["source"]
        for topic in topics
        for article in topic["top_articles"]
    }
    return {
        "days": days,
        "covered_days": covered_days,
        "partial": covered_days < days,
        "start_date": start.isoformat(),
        "end_date": as_of.isoformat(),
        "basis": basis,
        "article_count": len(selected),
        "sources": sorted(visible_sources),
        "topics": topics,
    }


def build_trends(articles: list[dict[str, Any]], now: datetime | None = None) -> dict[str, Any]:
    now = (now or _utc_now()).astimezone(timezone.utc)
    as_of = max((article["date"] for article in articles), default=now.date())
    return {
        "generated_at": now.isoformat(),
        "as_of": as_of.isoformat(),
        "ranges": {str(days): _aggregate_range(articles, days, as_of) for days in RANGES},
    }


def _json_text(value: Any, *, pretty: bool = True) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2 if pretty else None, sort_keys=False) + ("\n" if pretty else "")


def _inline_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def _display(value: Any, suffix: str = "") -> str:
    if value is None:
        return "未知"
    if isinstance(value, bool):
        return "是" if value else "否"
    return f"{value}{suffix}"


def _status_class(status: str) -> str:
    if status in {"healthy", "success"}:
        return "good"
    if status in {"degraded", "warning", "pending", "skipped"}:
        return "warn"
    if status in {"down", "failed"}:
        return "bad"
    return "unknown"


def _status_label(status: str) -> str:
    return {
        "healthy": "正常",
        "success": "成功",
        "degraded": "需关注",
        "warning": "警告",
        "pending": "进行中",
        "skipped": "已跳过",
        "disabled": "已停用",
        "down": "故障",
        "failed": "失败",
        "unknown": "未知",
    }.get(status, status)


def _base_css() -> str:
    return """
    :root { --ink:#171717; --paper:#fffdf0; --red:#ff3b30; --yellow:#ffd60a; --blue:#246bfd; --green:#25c46a; --muted:#eeeadd; --white:#fff; }
    * { box-sizing:border-box; }
    html { color-scheme:light; }
    body { margin:0; min-width:280px; color:var(--ink); background-color:var(--paper); background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='8' height='8' viewBox='0 0 8 8'%3E%3Ccircle cx='1' cy='1' r='1' fill='%23171717' fill-opacity='.13'/%3E%3C/svg%3E"); font-family:Arial,Helvetica,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; font-size:16px; line-height:1.5; }
    a { color:inherit; }
    .skip-link { position:absolute; left:12px; top:-80px; z-index:10; border:3px solid var(--ink); background:var(--yellow); padding:10px 14px; font-weight:900; }
    .skip-link:focus { top:12px; }
    .shell { width:min(1180px,calc(100% - 32px)); margin:0 auto; padding:20px 0 48px; }
    .masthead { border:4px solid var(--ink); box-shadow:10px 10px 0 var(--ink); background:var(--red); padding:18px; margin-bottom:26px; }
    .masthead-row { display:flex; align-items:flex-start; justify-content:space-between; gap:18px; flex-wrap:wrap; }
    h1,h2,h3,p { margin-top:0; }
    h1 { margin-bottom:4px; font-family:Impact,'Arial Black',sans-serif; font-size:clamp(36px,7vw,72px); line-height:.95; letter-spacing:0; text-transform:uppercase; }
    .subtitle { margin:8px 0 0; font-weight:800; }
    nav { display:flex; gap:10px; flex-wrap:wrap; }
    .button,button,select,input { min-height:44px; border:3px solid var(--ink); border-radius:0; background:var(--white); color:var(--ink); box-shadow:4px 4px 0 var(--ink); font:inherit; font-weight:900; }
    .button { display:inline-flex; align-items:center; padding:8px 12px; text-decoration:none; }
    button { padding:8px 14px; cursor:pointer; touch-action:manipulation; }
    select,input { width:100%; padding:8px 10px; }
    .button:hover,button:hover { background:var(--yellow); transform:translate(-1px,-1px); box-shadow:5px 5px 0 var(--ink); }
    :focus-visible { outline:4px solid var(--blue); outline-offset:4px; }
    .panel { border:4px solid var(--ink); box-shadow:7px 7px 0 var(--ink); background:var(--white); padding:18px; }
    .eyebrow { margin:0 0 6px; font-size:13px; font-weight:900; text-transform:uppercase; }
    .badge { display:inline-flex; align-items:center; min-height:34px; border:3px solid var(--ink); padding:3px 9px; font-weight:900; box-shadow:3px 3px 0 var(--ink); }
    .badge.good { background:var(--green); } .badge.warn { background:var(--yellow); } .badge.bad { background:var(--red); color:var(--white); } .badge.unknown { background:var(--muted); }
    .meta { color:#454545; font-weight:700; }
    .empty { border:4px dashed var(--ink); padding:22px; background:var(--paper); font-weight:900; }
    @media (max-width:640px) { .shell { width:min(100% - 20px,1180px); padding-top:10px; } .masthead { box-shadow:6px 6px 0 var(--ink); } nav { width:100%; } nav .button { flex:1 1 calc(50% - 10px); justify-content:center; } }
    @media (prefers-reduced-motion:reduce) { *,*::before,*::after { scroll-behavior:auto!important; transition:none!important; animation:none!important; } .button:hover,button:hover { transform:none; } }
    """


def _nav(current: str) -> str:
    links = (("首页", "index.html", "home"), ("历史", "archive.html", "archive"), ("状态", "status.html", "status"), ("趋势", "trends.html", "trends"), ("候选源", "sources.html", "sources"))
    rendered = []
    for label, href, key in links:
        current_attribute = ' aria-current="page"' if key == current else ""
        rendered.append(f'<a class="button" href="{href}"{current_attribute}>{label}</a>')
    return "".join(rendered)


def _status_time(value: Any) -> str:
    """Format status timestamps in Shanghai; naive input follows the UTC parser."""
    candidate = _iso_time(value)
    parsed = _parse_datetime(candidate)
    if parsed is None:
        return "未知"
    try:
        return parsed.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, ValueError):
        return "未知"


def _status_integer(value: Any, suffix: str = "") -> str:
    """Round display-only durations and intervals without changing machine data."""
    number = _number(value)
    return "未知" if number is None else f"{number:.0f}{suffix}"


def render_status_html(health: dict[str, Any], pilot: dict[str, Any]) -> str:
    collection = health["collection"]
    notification = health["notification"]
    publish = health["publish"]
    schedule = health["schedule"]
    overall = str(health["overall_status"])
    podcast = health.get("podcast", {})
    quality = health.get("quality", {})
    cards = [
        ("采集", "COLLECT", _status_label("success" if collection["success"] is True else "failed" if collection["success"] is False else "unknown"), _status_class("success" if collection["success"] is True else "failed" if collection["success"] is False else "unknown"), [("最近开始", _status_time(collection.get("last_started_at"))), ("最近完成", _status_time(collection.get("last_completed_at"))), ("耗时", _status_integer(collection["duration_seconds"], " 秒")), ("成功来源", collection["source_success"]), ("失败来源", collection["source_failed"]), ("条目", collection["item_count"]), ("间隔", _status_integer(collection["gap_minutes"], " 分钟")), ("补采", _display(collection["catch_up"]))]),
        ("飞书推送", "FEISHU", _status_label(notification["status"]), _status_class(notification["status"]), [("最近尝试", _status_time(notification.get("last_attempt_at"))), ("最近成功", _status_time(notification.get("last_success_at"))), ("原因", notification["reason"])]),
        ("AI 播客", "PODCAST", _status_label(podcast.get("status", "unknown")), _status_class(podcast.get("status", "unknown")), [("最新一期", podcast.get("date")), ("生成时间", _status_time(podcast.get("generated_at"))), ("音色", podcast.get("voice"))]),
        ("发布", "PUBLISH", _status_label(publish["status"]), _status_class(publish["status"]), [("最近尝试", _status_time(publish.get("last_attempt_at"))), ("最近成功", _status_time(publish.get("last_success_at"))), ("提交", publish["commit"])]),
        ("调度", "SCHEDULE", "每 %s 分钟" % _status_integer(schedule["interval_minutes"]), "good" if schedule.get("next_expected_at") else "unknown", [("下次预期", _status_time(schedule.get("next_expected_at")))]),
    ]
    cards[0][4].extend([
        ("统计日期", quality.get("date")),
        ("推荐相关度（AI自评均分）", _display(quality.get("mean_relevance_percent"), "%")),
        ("有效推荐占采集比例", _display(quality.get("selection_rate_percent"), "%")),
        ("推荐 / 原始采集", str(quality.get("accepted_count", 0)) + " / " + str(quality.get("collected_count", 0))),
        ("已识别周边标题", quality.get("peripheral_count", 0)),
        ("统计口径", "当天按规范化标题去重；AI分数是标题相关性自评，非人工准确率；未分类原始新闻也计入采集分母"),
    ])
    dedup = health.get('deduplication', {})
    collection_dedup = dedup.get('collection', {})
    delivery_dedup = dedup.get('delivery', {})
    cards.append(('重复检查', 'DEDUPLICATION', '精确去重', 'good', [
        ('最近采集检测', _status_time(collection_dedup.get('measured_at'))),
        ('本轮重复率', _display(collection_dedup.get('duplicate_rate_percent'), '%')),
        ('本轮原始条目', collection_dedup.get('candidate_count')),
        ('本轮重复条目', collection_dedup.get('repeat_count')),
        ('跨来源重复数', collection_dedup.get('cross_source_duplicate_count')),
        ('语义相似候选对', collection_dedup.get('semantic_candidate_pairs')),
        ('最近增量推送检测', _status_time(delivery_dedup.get('measured_at'))),
        ('已成功推送重复拦截数', delivery_dedup.get('delivered_blocked_count')),
        ('说明', '重复率含历史已采集或本轮同标题；长时间热榜重复不代表故障。推送拦截按群记录成功送达；语义相似只观察，不拦截事件新进展。未知表示尚未产生检测记录。'),
    ]))
    pilot_labels = {
        "not_configured": ("未启用", "unknown"),
        "observing": ("观察中", "warn"),
        "hold": ("暂缓晋级", "warn"),
        "ready": ("可晋级", "good"),
    }
    recommendation_labels = {
        "not_configured": "未配置",
        "observing": "观察中",
        "hold": "暂缓",
        "promote": "可晋级",
    }
    pilot_label, pilot_class = pilot_labels.get(pilot["status"], (pilot["status"], "unknown"))
    successful_sources = sum(source["success"] is True for source in pilot["sources"])
    total_items = sum(source["item_count"] or 0 for source in pilot["sources"])
    pilot_rows = [
        ("最近探测", _status_time(pilot.get("generated_at"))),
        ("可用来源", f"{successful_sources} / {len(pilot['sources'])}"),
        ("候选条目", total_items),
    ]
    for source in pilot["sources"]:
        state = "成功" if source["success"] is True else "失败" if source["success"] is False else "未知"
        detail = f"{state} · {recommendation_labels.get(source['recommendation'], source['recommendation'])}"
        if source["sample_days"] is not None:
            detail += f" · {source['sample_days']} 天"
        if source["item_count"] is not None:
            detail += f" · {source['item_count']} 条"
        if source["error"]:
            detail += f" · {source['error']}"
        if source["http_status"]:
            detail += f" {source['http_status']}"
        pilot_rows.append((source["source"], detail))
    cards.append(("候选源灰度", "SOURCE PILOT", pilot_label, pilot_class, pilot_rows))
    card_html = []
    for title, eyebrow, label, klass, rows in cards:
        details = "".join(f'<div><dt>{html.escape(str(name))}</dt><dd>{html.escape(_display(value))}</dd></div>' for name, value in rows)
        card_html.append(f'<section class="panel status-card"><p class="eyebrow">{eyebrow}</p><div class="card-head"><h2>{title}</h2><span class="badge {klass}">{html.escape(label)}</span></div><dl>{details}</dl></section>')
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>TrendRadar 运行状态</title><link rel="icon" type="image/png" sizes="32x32" href="/assets/brand/favicon-32.png"><link rel="apple-touch-icon" sizes="180x180" href="/assets/brand/apple-touch-icon.png"><style>{_base_css()}
    .overview {{ display:flex; align-items:center; justify-content:space-between; gap:16px; flex-wrap:wrap; margin-bottom:20px; background:var(--yellow); }}
    .overview h2 {{ margin-bottom:4px; font-size:clamp(24px,4vw,38px); }}
    .overview .badge {{ min-height:44px; font-size:18px; }}
    .status-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:20px; }}
    .card-head {{ display:flex; justify-content:space-between; align-items:center; gap:12px; }} .card-head h2 {{ margin:0; font-size:26px; }}
    dl {{ margin:18px 0 0; }} dl div {{ display:grid; grid-template-columns:minmax(110px,.7fr) minmax(0,1.3fr); gap:12px; padding:10px 0; border-top:3px solid var(--ink); }} dt {{ font-weight:900; }} dd {{ margin:0; overflow-wrap:anywhere; }}
    @media(max-width:760px) {{ .status-grid {{ grid-template-columns:1fr; }} }}
    </style></head><body><a class="skip-link" href="#main-content">跳到正文</a><main class="shell" id="main-content"><header class="masthead"><div class="masthead-row"><div><h1>System Status</h1><p class="subtitle">TrendRadar 采集、通知与发布状态 · 北京时间（Asia/Shanghai）</p></div><nav aria-label="主导航">{_nav("status")}</nav></div></header>
    <section class="panel overview"><div><p class="eyebrow">OVERALL</p><h2>系统总览</h2><p class="meta">生成于 {html.escape(_status_time(health.get("generated_at")))}</p></div><span class="badge {_status_class(overall)}">{html.escape(_status_label(overall))}</span></section>
    <div class="status-grid">{"".join(card_html)}</div></main></body></html>"""


def render_trends_html(trends: dict[str, Any]) -> str:
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>TrendRadar 趋势洞察</title><link rel="icon" type="image/png" sizes="32x32" href="/assets/brand/favicon-32.png"><link rel="apple-touch-icon" sizes="180x180" href="/assets/brand/apple-touch-icon.png"><style>{_base_css()}
    .controls {{ display:grid; grid-template-columns:auto minmax(180px,1fr) minmax(160px,.55fr); gap:16px; align-items:end; margin-bottom:22px; background:var(--yellow); }}
    .range-switch {{ display:flex; gap:8px; flex-wrap:wrap; }} .range-switch button[aria-pressed="true"] {{ background:var(--blue); color:var(--white); }}
    label {{ display:grid; gap:5px; font-weight:900; }}
    .summary {{ display:flex; gap:10px; flex-wrap:wrap; margin-bottom:20px; }} .summary .badge:nth-child(1) {{ background:var(--red); color:white; }} .summary .badge:nth-child(2) {{ background:var(--blue); color:white; }} .summary .badge:nth-child(3) {{ background:var(--green); }}
    .topics {{ display:grid; gap:20px; }} .topic {{ padding:0; overflow:hidden; }}
    .topic-head {{ padding:16px 18px; border-bottom:4px solid var(--ink); background:var(--yellow); }} .topic-head h2 {{ margin:0; font-size:clamp(23px,4vw,34px); overflow-wrap:anywhere; }}
    .stats {{ display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); border-bottom:4px solid var(--ink); }} .stat {{ padding:12px; border-right:3px solid var(--ink); }} .stat:last-child {{ border-right:0; }} .stat strong {{ display:block; font-size:21px; }} .stat span {{ font-size:12px; font-weight:900; }}
    .articles {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); }} .article {{ min-width:0; padding:16px 18px; border-bottom:3px solid var(--ink); }} .article:nth-child(odd) {{ border-right:3px solid var(--ink); }} .article a {{ font-weight:900; text-decoration-thickness:3px; overflow-wrap:anywhere; }} .article a:hover {{ color:var(--blue); }} .article p {{ margin:6px 0 0; font-size:13px; font-weight:800; color:#454545; }}
    #noResults {{ display:none; }}
    @media(max-width:820px) {{ .controls {{ grid-template-columns:1fr 1fr; }} .range-switch {{ grid-column:1/-1; }} .stats {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} .stat {{ border-bottom:3px solid var(--ink); }} }}
    @media(max-width:600px) {{ .controls {{ grid-template-columns:1fr; }} .articles {{ grid-template-columns:1fr; }} .article:nth-child(odd) {{ border-right:0; }} }}
    </style></head><body><a class="skip-link" href="#main-content">跳到正文</a><main class="shell" id="main-content"><header class="masthead"><div class="masthead-row"><div><h1>Trend Signals</h1><p class="subtitle">AI 标签、热度排名与跨来源聚合</p></div><nav aria-label="主导航">{_nav("trends")}</nav></div></header>
    <section class="panel controls" aria-label="趋势筛选"><div><span class="eyebrow">时间范围</span><div class="range-switch" role="group" aria-label="选择时间范围"><button type="button" data-range="1" aria-pressed="true">1 日</button><button type="button" data-range="7" aria-pressed="false">7 日</button><button type="button" data-range="30" aria-pressed="false">30 日</button><button type="button" data-range="90" aria-pressed="false">90 日</button></div></div><label for="keyword">关键词<input id="keyword" type="search" placeholder="搜索主题或文章"></label><label for="source">平台<select id="source"><option value="">全部平台</option></select></label></section>
    <div class="summary" id="summary" aria-live="polite"></div><div class="topics" id="topics"></div><p class="empty" id="noResults">没有符合筛选条件的趋势。</p></main>
    <script id="trend-data" type="application/json">{_inline_json(trends)}</script><script>
    (() => {{
      const data=JSON.parse(document.getElementById('trend-data').textContent); let active='1';
      const topics=document.getElementById('topics'), keyword=document.getElementById('keyword'), source=document.getElementById('source'), summary=document.getElementById('summary'), empty=document.getElementById('noResults');
      const esc=value=>String(value??'').replace(/[&<>"']/g,ch=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));
      function populateSources() {{ const current=source.value; source.innerHTML='<option value="">全部平台</option>'; (data.ranges[active].sources||[]).forEach(name=>{{const option=document.createElement('option');option.value=name;option.textContent=name;source.appendChild(option);}}); source.value=Array.from(source.options).some(o=>o.value===current)?current:''; }}
      function render() {{ const range=data.ranges[active], q=keyword.value.trim().toLocaleLowerCase(), platform=source.value; let shown=0; topics.innerHTML='';
        range.topics.forEach(topic=>{{ const matchesTag=topic.tag.toLocaleLowerCase().includes(q); const articles=topic.top_articles.filter(a=>(!platform||a.source===platform)&&(!q||matchesTag||a.title.toLocaleLowerCase().includes(q)||a.source.toLocaleLowerCase().includes(q))); if(!articles.length)return; shown++;
          const relevance=topic.average_relevance===null?'—':Math.round(topic.average_relevance*100)+'%'; const rank=topic.best_rank===null?'—':'#'+topic.best_rank; const momentum=(topic.momentum>0?'+':'')+topic.momentum;
          const articleHtml=articles.map(a=>`<article class="article"><a href="${{esc(a.url||'#')}}"${{a.url?' target="_blank" rel="noopener noreferrer"':''}}>${{esc(a.title)}}</a><p>${{esc(a.source)}} · ${{esc(a.date)}} · 最高 ${{a.best_rank===null?'—':'#'+a.best_rank}}</p></article>`).join('');
          topics.insertAdjacentHTML('beforeend',`<section class="panel topic"><div class="topic-head"><p class="eyebrow">${{topic.basis==='ai_filter'?'AI FILTER':'PLATFORM FALLBACK'}}</p><h2>${{esc(topic.tag)}}</h2></div><div class="stats"><div class="stat"><strong>${{topic.article_count}}</strong><span>文章</span></div><div class="stat"><strong>${{topic.source_count}}</strong><span>来源</span></div><div class="stat"><strong>${{relevance}}</strong><span>平均相关度</span></div><div class="stat"><strong>${{rank}}</strong><span>最佳排名</span></div><div class="stat"><strong>${{momentum}}</strong><span>排名动量</span></div></div><div class="articles">${{articleHtml}}</div></section>`);
        }});
        summary.innerHTML=`<span class="badge">${{active}} 日窗口</span><span class="badge">覆盖 ${{range.covered_days}} / ${{range.days}} 天</span><span class="badge">截至 ${{esc(range.end_date)}}</span><span class="badge">${{range.basis==='ai_filter'?'AI 标签聚合':'平台回退聚合'}}</span><span class="badge">显示 ${{shown}} 个主题</span>`; empty.style.display=shown?'none':'block';
      }}
      document.querySelectorAll('[data-range]').forEach(button=>button.addEventListener('click',()=>{{active=button.dataset.range;document.querySelectorAll('[data-range]').forEach(item=>item.setAttribute('aria-pressed',String(item===button)));populateSources();render();}})); keyword.addEventListener('input',render); source.addEventListener('change',render); populateSources(); render();
    }})();
    </script></body></html>"""


def render_sources_html(feed: dict[str, Any]) -> str:
    groups = []
    for group in feed["sources"]:
        articles = []
        for item in group["items"]:
            href = html.escape(item["url"] or "#", quote=True)
            target = ' target="_blank" rel="noopener noreferrer"' if item["url"] else ""
            badges = []
            if item["interest_rules"]:
                badges.append('<span class="mini hit">兴趣命中</span>')
            if item["duplicate_in_official_news"]:
                badges.append('<span class="mini duplicate">正式库重复</span>')
            meta_html = "".join(badges) or '<span class="mini">观察样本</span>'
            articles.append(
                f'<article class="source-item"><a href="{href}"{target}>{html.escape(item["title"])}</a>'
                f'<div class="item-meta">{meta_html}</div></article>'
            )
        groups.append(
            f'<section class="panel source-group"><div class="source-head"><div><p class="eyebrow">CANDIDATE SOURCE</p>'
            f'<h2>{html.escape(group["source"])}</h2></div><span class="badge">{len(group["items"])} 条</span></div>'
            f'<div class="source-items">{"".join(articles) or "<p>本轮没有可展示条目。</p>"}</div></section>'
        )
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>TrendRadar 候选源观察</title><link rel="icon" type="image/png" sizes="32x32" href="/assets/brand/favicon-32.png"><link rel="apple-touch-icon" sizes="180x180" href="/assets/brand/apple-touch-icon.png"><style>{_base_css().rstrip()}
    .notice {{ margin-bottom:20px; background:var(--yellow); }} .notice p {{ margin:6px 0 0; font-weight:800; }}
    .source-group {{ margin-bottom:22px; }} .source-head {{ display:flex; align-items:center; justify-content:space-between; gap:12px; }} .source-head h2 {{ margin:0; font-size:28px; overflow-wrap:anywhere; }}
    .source-items {{ margin-top:16px; }} .source-item {{ display:grid; grid-template-columns:minmax(0,1fr) auto; gap:14px; align-items:center; padding:12px 0; border-top:3px solid var(--ink); }}
    .source-item a {{ color:var(--ink); font-weight:900; line-height:1.45; }} .source-item a:hover {{ color:var(--blue); }} .item-meta {{ display:flex; gap:6px; flex-wrap:wrap; justify-content:flex-end; }}
    .mini {{ display:inline-block; padding:3px 6px; border:2px solid var(--ink); background:#fff; font-size:11px; font-weight:900; }} .mini.hit {{ background:var(--green); }} .mini.duplicate {{ background:var(--red); color:#fff; }}
    @media(max-width:700px) {{ .source-item {{ grid-template-columns:1fr; }} .item-meta {{ justify-content:flex-start; }} }}
    </style></head><body><a class="skip-link" href="#main-content">跳到正文</a><main class="shell" id="main-content"><header class="masthead"><div class="masthead-row"><div><h1>Source Watch</h1><p class="subtitle">稳定性、兴趣命中与重复率灰度观察</p></div><nav aria-label="主导航">{_nav("sources")}</nav></div></header>
    <section class="panel notice"><p class="eyebrow">ISOLATED PILOT</p><h2>候选源观察区</h2><p>这里的内容不会进入正式首页或飞书推送。连续观察达到晋级阈值后，再人工确认是否并入正式新闻源。</p><p class="meta">最近探测：{html.escape(_display(feed["generated_at"]))}</p></section>{"".join(groups)}</main></body></html>"""


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temporary = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def build_quality_metrics(articles, day, minimum=0.7):
    unique = {}
    for article in articles:
        if article["date"] != day:
            continue
        title = re.sub(r"\s+", "", article["title"]).casefold()
        score = max((float(score) for _tag, score in article.get("tags", [])), default=0)
        if title not in unique or score > unique[title][1]:
            unique[title] = (article["title"], score)
    accepted = [score for title, score in unique.values() if score >= minimum and not peripheral_reason(title)]
    return {
        "date": day.isoformat(), "collected_count": len(unique), "accepted_count": len(accepted),
        "mean_relevance_percent": round(sum(accepted) / len(accepted) * 100, 1) if accepted else None,
        "selection_rate_percent": round(len(accepted) / len(unique) * 100, 1) if unique else None,
        "peripheral_count": sum(bool(peripheral_reason(title)) for title, _score in unique.values()),
        "minimum_score": minimum, "basis": "title_only_model_self_score_not_accuracy",
    }


def generate_site_insights(repo_dir: Path, site_dir: Path, now: datetime | None = None) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    output_dir = repo_dir / "TrendRadar" / "output"
    runtime = _load_json(output_dir / "meta" / "runtime_health.json") or {}
    podcast = _load_json(output_dir / "audio" / "latest.json") or {}
    expected = (now or _utc_now()).astimezone(ZoneInfo("Asia/Shanghai"))
    expected_day = expected.date() if expected.hour >= 22 else expected.date() - timedelta(days=1)
    audio_file = output_dir / str(podcast.get("audio_url", "")).lstrip("/")
    runtime["podcast"] = {
        "date": podcast.get("date"), "generated_at": podcast.get("generated_at"),
        "voice": podcast.get("voice_label", podcast.get("voice")),
        "status": "success" if podcast.get("date", "") >= expected_day.isoformat() and audio_file.is_file() and audio_file.stat().st_size > 1000 else "failed",
    }
    health = build_public_health(runtime, now=now)
    collection_dedup = _load_json(output_dir / 'meta/collection_dedup.json') or {}
    deliveries = (_load_json(output_dir / 'meta/delivery_dedup.json') or {}).get('accounts', {})
    delivery_dedup = max(deliveries.values(), key=lambda item: _parse_datetime(item.get('measured_at')) or datetime.min.replace(tzinfo=timezone.utc), default={})
    health['deduplication'] = {
        'collection': {key: collection_dedup.get(key) for key in ('measured_at', 'candidate_count', 'repeat_count', 'duplicate_rate_percent', 'cross_source_duplicate_count', 'semantic_candidate_pairs')},
        'delivery': {key: delivery_dedup.get(key) for key in ('measured_at', 'delivered_blocked_count', 'candidate_count', 'delivery_status')},
    }
    pilot = build_public_pilot(_load_json(output_dir / "source-pilot" / "latest.json"))
    source_feed = build_public_source_feed(_load_json(output_dir / "source-pilot" / "latest.json"))
    health["source_pilot"] = pilot
    articles, skipped = load_articles(output_dir)
    health["quality"] = build_quality_metrics(articles, expected.date())
    trends = build_trends([article for article in articles if not peripheral_reason(article['title'])], now=now)
    outputs = {
        site_dir / "health.json": _json_text(health),
        site_dir / "trends.json": _json_text(trends),
        site_dir / "status.html": render_status_html(health, pilot),
        site_dir / "trends.html": render_trends_html(trends),
        site_dir / "sources.json": _json_text(source_feed),
        site_dir / "sources.html": render_sources_html(source_feed),
    }
    for path, content in outputs.items():
        _atomic_write(path, content)
    return health, trends, skipped


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-dir", type=Path, default=REPO_DIR, help="repository root")
    parser.add_argument("--site-dir", type=Path, help="output directory (default: <repo>/docs)")
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    repo_dir = args.repo_dir.resolve()
    site_dir = args.site_dir.resolve() if args.site_dir else (repo_dir / "docs")
    _health, trends, skipped = generate_site_insights(repo_dir, site_dir)
    topic_counts = ", ".join(f"{days}d={len(value['topics'])}" for days, value in trends["ranges"].items())
    print(f"Generated site insights in {site_dir} ({topic_counts})")
    if skipped:
        print(f"Skipped {len(skipped)} unreadable database(s): " + "; ".join(skipped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
