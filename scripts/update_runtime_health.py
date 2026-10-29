#!/usr/bin/env python3
"""Maintain private runtime health state for the static status page generator."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


REPO_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = REPO_DIR / "TrendRadar" / "output"
STATE_FILE = OUTPUT_DIR / "meta" / "runtime_health.json"
SCHEDULE_MINUTES = 60
SLEEP_GAP_MINUTES = 90


def now_local() -> datetime:
    return datetime.now().astimezone()


def iso_time(value: datetime | None = None) -> str:
    return (value or now_local()).isoformat(timespec="seconds")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.astimezone()
    except (TypeError, ValueError):
        return None


def load_state() -> dict[str, Any]:
    try:
        value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, STATE_FILE)


def newest_db(directory: str) -> Path | None:
    candidates = sorted((OUTPUT_DIR / directory).glob("*.db"), reverse=True)
    return candidates[0] if candidates else None


def query_one(db_path: Path | None, sql: str) -> tuple[Any, ...] | None:
    if not db_path or not db_path.exists():
        return None
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2) as conn:
            return conn.execute(sql).fetchone()
    except sqlite3.Error:
        return None


def crawl_snapshot(started_at: datetime | None) -> dict[str, Any]:
    news_db = newest_db("news")
    rss_db = newest_db("rss")
    news_row = query_one(
        news_db,
        """
        SELECT cr.crawl_time, cr.total_items,
               SUM(CASE WHEN css.status = 'success' THEN 1 ELSE 0 END),
               SUM(CASE WHEN css.status = 'failed' THEN 1 ELSE 0 END),
               cr.created_at
        FROM crawl_records cr
        LEFT JOIN crawl_source_status css ON css.crawl_record_id = cr.id
        GROUP BY cr.id ORDER BY cr.id DESC LIMIT 1
        """,
    )
    rss_row = query_one(
        rss_db,
        """
        SELECT cr.crawl_time, cr.total_items,
               SUM(CASE WHEN rcs.status = 'success' THEN 1 ELSE 0 END),
               SUM(CASE WHEN rcs.status = 'failed' THEN 1 ELSE 0 END),
               cr.created_at
        FROM rss_crawl_records cr
        LEFT JOIN rss_crawl_status rcs ON rcs.crawl_record_id = cr.id
        GROUP BY cr.id ORDER BY cr.id DESC LIMIT 1
        """,
    )

    source_success = int((news_row or (None, 0, 0, 0))[2] or 0) + int((rss_row or (None, 0, 0, 0))[2] or 0)
    source_failed = int((news_row or (None, 0, 0, 0))[3] or 0) + int((rss_row or (None, 0, 0, 0))[3] or 0)
    item_count = int((news_row or (None, 0))[1] or 0) + int((rss_row or (None, 0))[1] or 0)
    database_updated = False
    if started_at:
        for path in (news_db, rss_db):
            if path and path.exists() and path.stat().st_mtime >= started_at.timestamp() - 5:
                database_updated = True
                break

    return {
        "source_success": source_success,
        "source_failed": source_failed,
        "item_count": item_count,
        "database_updated": database_updated,
        "crawl_time": (news_row or rss_row or (None,))[0],
    }


def notification_snapshot(log_path: Path | None) -> dict[str, str]:
    if not log_path or not log_path.exists():
        return {"status": "unknown", "reason": "run_log_unavailable"}
    text = log_path.read_text(encoding="utf-8", errors="replace")
    attempted = bool(re.search(r"飞书消息分为|发送飞书第", text))
    succeeded = bool(re.search(r"飞书所有\s*\d+\s*批次发送完成", text))
    failed = bool(re.search(r"飞书.*(?:发送失败|请求失败|异常)", text))
    if succeeded:
        return {"status": "success", "reason": "delivered"}
    if failed:
        return {"status": "failed", "reason": "delivery_failed"}
    if attempted:
        return {"status": "failed", "reason": "completion_not_confirmed"}
    if "通知功能已禁用" in text:
        return {"status": "disabled", "reason": "notification_disabled"}
    return {"status": "skipped", "reason": "no_incremental_match_or_schedule"}


def git_head() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short=12", "HEAD"], cwd=REPO_DIR, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def start_event(state: dict[str, Any]) -> None:
    now = now_local()
    collection = state.setdefault("collection", {})
    previous = parse_time(collection.get("last_completed_at"))
    gap_minutes = round((now - previous).total_seconds() / 60, 1) if previous else None
    collection.update(
        {
            "last_started_at": iso_time(now),
            "running": True,
            "success": None,
            "gap_minutes": gap_minutes,
            "catch_up": bool(gap_minutes and gap_minutes > SLEEP_GAP_MINUTES),
        }
    )
    state["schedule"] = {"interval_minutes": SCHEDULE_MINUTES}
    state["updated_at"] = iso_time(now)


def finish_event(state: dict[str, Any], exit_code: int, log_path: Path | None) -> None:
    now = now_local()
    collection = state.setdefault("collection", {})
    started = parse_time(collection.get("last_started_at"))
    crawl = crawl_snapshot(started)
    success = exit_code == 0 and crawl.pop("database_updated")
    collection.update(crawl)
    collection.update(
        {
            "last_completed_at": iso_time(now),
            "duration_seconds": round((now - started).total_seconds(), 1) if started else None,
            "running": False,
            "success": success,
            "exit_code": exit_code,
        }
    )

    notification = state.setdefault("notification", {})
    notification_result = notification_snapshot(log_path)
    if notification_result["status"] not in {"skipped", "disabled", "unknown"}:
        notification["last_attempt_at"] = iso_time(now)
    if notification_result["status"] == "success":
        notification["last_success_at"] = iso_time(now)
    notification.update(notification_result)
    if log_path and log_path.is_file():
        run_text = log_path.read_text(encoding='utf-8', errors='replace')
        if '跳过通知：增量模式下未检测到匹配的新闻和RSS' in run_text:
            # This is a measured zero-candidate run, not an inferred delivery success.
            metrics_path = OUTPUT_DIR / 'meta/delivery_dedup.json'
            try:
                metrics = json.loads(metrics_path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                metrics = {'accounts': {}}
            metrics.setdefault('accounts', {})['no_candidates'] = {
                'measured_at': iso_time(now), 'candidate_count': 0,
                'delivered_blocked_count': 0, 'delivery_status': 'skipped_no_candidates',
            }
            metrics_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = metrics_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(metrics, ensure_ascii=False, indent=2))
            temporary.replace(metrics_path)
    state["updated_at"] = iso_time(now)


def publish_event(state: dict[str, Any], success: bool) -> None:
    now = now_local()
    publish = state.setdefault("publish", {})
    publish.update(
        {
            "last_attempt_at": iso_time(now),
            "status": "success" if success else "failed",
            "commit": git_head(),
        }
    )
    if success:
        publish["last_success_at"] = iso_time(now)
    state["updated_at"] = iso_time(now)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="event", required=True)
    subparsers.add_parser("start")
    finish = subparsers.add_parser("finish")
    finish.add_argument("--exit-code", type=int, required=True)
    finish.add_argument("--run-log", type=Path)
    publish = subparsers.add_parser("publish")
    publish.add_argument("--success", action=argparse.BooleanOptionalAction, required=True)
    args = parser.parse_args()

    state = load_state()
    state.setdefault("schema_version", 1)
    if args.event == "start":
        start_event(state)
    elif args.event == "finish":
        finish_event(state, args.exit_code, args.run_log)
    else:
        publish_event(state, args.success)
    write_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
