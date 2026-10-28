#!/usr/bin/env python3
"""Build the evidence, event, search, recommendation and notification preview data."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import re
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from generate_site_insights import load_articles, peripheral_reason


REPO_DIR = Path(__file__).resolve().parents[1]
DEFAULT_SITE_DIR = REPO_DIR / "docs"
TRACKING_PARAMETERS = {
    "fbclid", "gclid", "ref", "referrer", "source", "spm", "utm_campaign",
    "utm_content", "utm_medium", "utm_source", "utm_term",
}
ENTITY_PATTERNS = (
    "OpenAI", "ChatGPT", "Anthropic", "Claude", "Google", "Gemini", "DeepSeek",
    "Meta", "Microsoft", "Apple", "NVIDIA", "英伟达", "华为", "小米", "阿里",
    "腾讯", "字节", "百度", "智谱", "月之暗面", "Kimi", "阶跃星辰", "MiniMax",
    "Grok", "MCP", "Agent", "Codex", "Cursor", "Qwen", "通义", "豆包",
)
EVENT_THEMES = (
    ("service_incident", r"宕机|故障|停摆|崩了|不可用|恢复服务|服务恢复|补偿|赔偿"),
    ("model_release", r"发布|上线|上新|新模型|新版本|开源|权重|预览版|preview"),
    ("funding", r"融资|募资|估值|投资|收购"),
    ("benchmark", r"评测|测试|实测|benchmark|sota|性能"),
    ("policy_legal", r"监管|起诉|诉讼|禁令|合规|安全|版权"),
    ("product", r"产品|功能|api|应用|工具|平台"),
    ("infrastructure", r"芯片|算力|gpu|数据中心|推理|训练|部署"),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    query = urlencode(
        sorted((key, item) for key, item in parse_qsl(parts.query, keep_blank_values=True)
               if key.lower() not in TRACKING_PARAMETERS)
    )
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path.rstrip("/") or "/", query, ""))


def _stable_id(prefix: str, *parts: str) -> str:
    payload = "\x1f".join(part.strip().lower() for part in parts if part)
    return f"{prefix}_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]}"


def _bounded(value: float, low: float = 0, high: float = 100) -> float:
    return round(max(low, min(high, value)), 1)


def _score(article: dict[str, Any], as_of: date) -> dict[str, Any]:
    rank = article.get("best_rank")
    rank_score = 0 if rank is None else max(0, 100 - (int(rank) - 1) * 3.2)
    relevance = max((float(score) for _tag, score in article.get("tags", [])), default=0)
    relevance_score = relevance * 100
    frequency_score = min(100, math.log2(max(1, int(article.get("crawl_count", 1)))) * 24)
    momentum_score = max(0, min(100, 50 + float(article.get("momentum", 0))))
    age = max(0, (as_of - article["date"]).days)
    freshness_score = max(0, 100 - age * 18)
    total = _bounded(
        rank_score * 0.30
        + relevance_score * 0.32
        + frequency_score * 0.14
        + momentum_score * 0.10
        + freshness_score * 0.14
    )
    return {
        "total": total,
        "components": {
            "rank": round(rank_score, 1),
            "interest": round(relevance_score, 1),
            "frequency": round(frequency_score, 1),
            "momentum": round(momentum_score, 1),
            "freshness": round(freshness_score, 1),
        },
        "reason": _score_reason(rank, relevance, article.get("crawl_count", 1), article.get("momentum", 0)),
    }


def _score_reason(rank: int | None, relevance: float, crawl_count: int, momentum: float) -> str:
    reasons = []
    if rank is not None and rank <= 5:
        reasons.append(f"最高热榜第 {rank} 名")
    if relevance >= 0.8:
        reasons.append("高度匹配你的兴趣")
    elif relevance >= 0.6:
        reasons.append("匹配你的兴趣")
    if crawl_count >= 5:
        reasons.append(f"连续出现 {crawl_count} 次")
    if momentum >= 20:
        reasons.append("热度明显上升")
    return "、".join(reasons[:2]) or "综合热度与时效入选"


def _entities(title: str) -> list[str]:
    lowered = title.casefold()
    found = [entity for entity in ENTITY_PATTERNS if entity.casefold() in lowered]
    ascii_names = re.findall(r"\b[A-Z][A-Za-z0-9.-]{2,}(?:\s+[A-Z][A-Za-z0-9.-]{2,})?\b", title)
    return list(dict.fromkeys(found + ascii_names))[:5]


def _event_key(article: dict[str, Any]) -> tuple[str, str]:
    entities = _entities(article["title"])
    tags = article.get("tags") or []
    first_tag = tags[0] if tags else None
    tag = first_tag.get("name", "其他") if isinstance(first_tag, dict) else first_tag[0] if first_tag else "其他"
    if entities:
        lowered = article["title"].casefold()
        theme = next((name for name, pattern in EVENT_THEMES if re.search(pattern, lowered, re.I)), None)
        if theme:
            return tag, f"{entities[0].casefold()}:{theme}"
        remainder = lowered.replace(entities[0].casefold(), "")
        fingerprint = re.sub(r"[^\w\u4e00-\u9fff]+", "", remainder)[:14]
        return tag, f"{entities[0].casefold()}:{fingerprint}"
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "", article["title"].casefold())
    return tag, normalized[:18]


def _article_record(article: dict[str, Any], as_of: date) -> dict[str, Any]:
    canonical_url = _normalize_url(article.get("url"))
    identity = canonical_url or re.sub(r"\s+", "", article["title"])
    article_id = _stable_id("news", identity, article["source_id"])
    score = _score(article, as_of)
    tags = [{"name": tag, "relevance": round(float(value), 3)} for tag, value in article.get("tags", [])]
    return {
        "id": article_id,
        "title": article["title"],
        "source_id": article["source_id"],
        "source": article["source"],
        "date": article["date"].isoformat(),
        "published_at": article.get("published_at"),
        "first_seen_at": article.get("first_seen_at"),
        "last_seen_at": article.get("last_seen_at"),
        "url": canonical_url,
        "report_url": f"/archive.html?date={article['date'].isoformat()}",
        "summary": article.get("summary"),
        "best_rank": article.get("best_rank"),
        "momentum": article.get("momentum", 0),
        "crawl_count": article.get("crawl_count", 1),
        "tags": tags,
        "entities": _entities(article["title"]),
        "score": score,
    }


def _events(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        buckets[_event_key(record)].append(record)
    result = []
    for (tag, subject), items in buckets.items():
        items.sort(key=lambda item: (item["date"], item["score"]["total"]), reverse=True)
        event_id = _stable_id("event", tag, subject)
        sources = sorted({item["source"] for item in items})
        top = max(items, key=lambda item: item["score"]["total"])
        result.append({
            "id": event_id,
            "title": top["title"],
            "topic": tag,
            "subject": subject,
            "status": "developing" if len({item["date"] for item in items}) > 1 else "observed",
            "first_seen": min(item["date"] for item in items),
            "last_seen": max(item["date"] for item in items),
            "source_count": len(sources),
            "sources": sources,
            "article_count": len(items),
            "importance": max(item["score"]["total"] for item in items),
            "summary": f"{tag} 相关事件，已收录 {len(items)} 条报道，来自 {len(sources)} 个来源。",
            "analysis": {
                "type": "inference",
                "statement": "该主题持续出现" if len(items) > 1 else "该主题刚进入观察范围",
                "confidence": "medium" if len(items) > 1 else "low",
                "evidence_ids": [item["id"] for item in items[:5]],
                "counterpoint": "当前仅基于已收录标题、热榜和来源数据，不能替代原文事实核验。",
            },
            "article_ids": [item["id"] for item in items],
        })
    result.sort(key=lambda event: (event["importance"], event["last_seen"], event["article_count"]), reverse=True)
    return result


def _deduplicate_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for record in sorted(records, key=lambda item: item["date"]):
        existing = merged.get(record["id"])
        if existing is None:
            record["first_date"] = record["date"]
            record["last_date"] = record["date"]
            record["appearances"] = 1
            merged[record["id"]] = record
            continue
        existing["last_date"] = max(existing["last_date"], record["date"])
        existing["first_date"] = min(existing["first_date"], record["date"])
        existing["appearances"] += 1
        existing["crawl_count"] = max(existing["crawl_count"], record["crawl_count"])
        if record["date"] >= existing["date"]:
            for key in ("date", "published_at", "first_seen_at", "last_seen_at", "report_url", "best_rank", "momentum", "score"):
                existing[key] = record[key]
            if record["summary"]:
                existing["summary"] = record["summary"]
        combined_tags = {tag["name"]: tag for tag in existing["tags"]}
        for tag in record["tags"]:
            if tag["name"] not in combined_tags or tag["relevance"] > combined_tags[tag["name"]]["relevance"]:
                combined_tags[tag["name"]] = tag
        existing["tags"] = sorted(combined_tags.values(), key=lambda item: item["relevance"], reverse=True)
    return list(merged.values())


def _notification_preview(events: list[dict[str, Any]], records_by_id: dict[str, dict[str, Any]], as_of: str) -> dict[str, Any]:
    candidates = []
    events_by_id = {event["id"]: event for event in events}
    for event in events:
        if event["last_seen"] != as_of:
            continue
        members = [records_by_id[item_id] for item_id in event["article_ids"] if item_id in records_by_id]
        high_interest = any(item["score"]["components"]["interest"] >= 80 for item in members)
        top_rank = any((item["best_rank"] or 999) <= 5 for item in members)
        multi_source = event["source_count"] >= 2
        rising = any(float(item["momentum"]) >= 20 for item in members)
        conditions = {
            "high_interest": high_interest,
            "top_rank": top_rank,
            "multi_source": multi_source,
            "rising": rising,
        }
        met = sum(conditions.values())
        if met >= 2 and event["importance"] >= 60:
            candidates.append({
                "event_id": event["id"],
                "title": event["title"],
                "conditions": conditions,
                "matched_conditions": met,
                "recommendation": "high_value_increment",
            })
    candidates.sort(key=lambda item: events_by_id.get(item["event_id"], {}).get("importance", 0), reverse=True)
    selected = candidates[:3]
    return {
        "mode": "shadow_only",
        "changes_delivery": False,
        "policy": "at_least_two_conditions_and_importance_60",
        "daily_cap": 3,
        "date": as_of,
        "eligible_count": len(candidates),
        "candidate_count": len(selected),
        "candidates": selected,
    }


def build_experience(repo_dir: Path, now: datetime | None = None) -> dict[str, Any]:
    now = now or _now()
    raw_articles, skipped = load_articles(repo_dir / "TrendRadar" / "output")
    as_of = max((item["date"] for item in raw_articles), default=now.date())
    all_records = _deduplicate_records([_article_record(item, as_of) for item in raw_articles])
    all_events = _events(all_records)
    events = [
        event for event in all_events
        if event["topic"] != "其他"
        and (event["last_seen"] == as_of.isoformat() or event["article_count"] > 1)
    ][:1000]
    event_by_article = {
        article_id: event["id"]
        for event in events
        for article_id in event["article_ids"]
    }
    for record in all_records:
        record["event_id"] = event_by_article.get(record["id"])

    today = [record for record in all_records if record["date"] == as_of.isoformat()]
    included_ids = {record["id"] for record in today}
    included_ids.update(article_id for event in events for article_id in event["article_ids"])
    records = [record for record in all_records if record["id"] in included_ids]
    records_by_id = {record["id"]: record for record in records}
    recommended = [record for record in today if not peripheral_reason(record["title"])
                   and max((tag["relevance"] for tag in record["tags"]), default=0) >= 0.7]
    global_important = sorted(recommended, key=lambda item: item["score"]["total"], reverse=True)[:8]
    personalized = sorted(
        recommended,
        key=lambda item: (item["score"]["components"]["interest"], item["score"]["total"]),
        reverse=True,
    )[:8]
    claims = []
    for event in [item for item in events if item["last_seen"] == as_of.isoformat()][:5]:
        claims.append({
            "id": _stable_id("claim", event["id"], as_of.isoformat()),
            "type": "inference",
            "statement": event["analysis"]["statement"],
            "event_id": event["id"],
            "evidence_ids": event["analysis"]["evidence_ids"],
            "confidence": event["analysis"]["confidence"],
            "counterpoint": event["analysis"]["counterpoint"],
        })
    return {
        "schema_version": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "as_of": as_of.isoformat(),
        "coverage": {
            "article_count": len(all_records),
            "interactive_article_count": len(records),
            "event_count": len(all_events),
            "interactive_event_count": len(events),
            "skipped": skipped,
        },
        "recommendations": {
            "global_important": [item["id"] for item in global_important],
            "for_you": [item["id"] for item in personalized],
        },
        "claims": claims,
        "events": events,
        "articles": records,
        "notification_preview": _notification_preview(events, records_by_id, as_of.isoformat()),
        "search_index": [
            {
                "id": record["id"],
                "title": record["title"],
                "source": record["source"],
                "date": record["date"],
                "url": record["url"],
                "report_url": record["report_url"],
                "entities": record["entities"],
                "tags": [tag["name"] for tag in record["tags"]],
                "event_id": record["event_id"],
                "score": {"total": record["score"]["total"], "reason": record["score"]["reason"]},
            }
            for record in all_records
        ],
    }


def _base_style() -> str:
    return """
    :root{--ink:#171717;--paper:#fffdf0;--red:#ff3b30;--yellow:#ffd60a;--blue:#246bfd;--white:#fff;--muted:#eeeadd}
    *{box-sizing:border-box}body{margin:0;min-width:280px;color:var(--ink);background-color:var(--paper);background-image:radial-gradient(circle,rgba(23,23,23,.15) 1px,transparent 1px);background-size:5px 5px;font-family:Arial,Helvetica,sans-serif;font-weight:700;line-height:1.5}
    .shell{width:min(1180px,calc(100% - 32px));margin:auto;padding:20px 0 48px}.mast{background:var(--red);border:4px solid var(--ink);box-shadow:10px 10px 0 var(--ink);padding:20px;margin-bottom:26px}.mast h1{font-family:Impact,'Arial Black',sans-serif;font-size:clamp(38px,7vw,70px);line-height:.95;margin:0 0 12px}.nav{display:flex;gap:10px;flex-wrap:wrap}.button,button,input,select{min-height:44px;border:3px solid var(--ink);border-radius:0;background:var(--white);box-shadow:4px 4px 0 var(--ink);color:var(--ink);font:inherit;font-weight:900;padding:9px 12px}.button{text-decoration:none;display:inline-flex;align-items:center}.panel{background:var(--white);border:4px solid var(--ink);box-shadow:7px 7px 0 var(--ink);padding:18px;margin-bottom:22px}.eyebrow{font-size:12px;font-weight:900;margin:0 0 4px}.panel h2{font-size:clamp(22px,4vw,34px);margin:0 0 8px}.meta{color:#4a4a4a;margin:5px 0}.evidence{display:grid;gap:10px;margin-top:14px}.article{border-top:3px solid var(--ink);padding-top:10px}.article a{color:var(--blue);font-weight:900}.badge{display:inline-block;border:3px solid var(--ink);background:var(--yellow);padding:3px 7px;font-size:12px}.search{width:100%;margin-bottom:18px}.hidden{display:none!important}@media(max-width:640px){.shell{width:min(100% - 20px,1180px)}.mast{padding:16px}.panel{padding:14px}}
    """


def _events_html() -> str:
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>NewsRadar 事件追踪</title><link rel="icon" href="/assets/brand/favicon-32.png"><style>{_base_style()}</style></head><body><main class="shell"><header class="mast"><h1>EVENT TRACKER</h1><nav class="nav"><a class="button" href="/">首页</a><a class="button" href="/archive.html">历史</a><a class="button" href="/search.html">搜索</a><a class="button" href="/trends.html">趋势</a></nav></header><input class="search" id="query" type="search" placeholder="搜索事件、主题或来源" aria-label="搜索事件"><div id="events"></div></main><script>
    const esc=v=>String(v??'').replace(/[&<>\"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}}[c]));
    fetch('/experience.json',{{cache:'no-store'}}).then(r=>r.json()).then(data=>{{const articles=new Map(data.articles.map(a=>[a.id,a]));const root=document.getElementById('events');const render=()=>{{const q=document.getElementById('query').value.trim().toLowerCase();const requested=location.hash.slice(1);root.innerHTML='';let matches=data.events.filter(e=>!q||[e.title,e.topic,e.subject,...e.sources].join(' ').toLowerCase().includes(q));if(requested&&!q)matches.sort((a,b)=>(b.id===requested)-(a.id===requested));const visible=matches.slice(0,100);visible.forEach(e=>{{const evidence=e.article_ids.slice(0,8).map(id=>articles.get(id)).filter(Boolean).map(a=>`<article class="article"><a href="${{esc(a.url||a.report_url)}}" target="_blank" rel="noopener noreferrer">${{esc(a.title)}}</a><p class="meta">${{esc(a.source)}} · ${{esc(a.date)}} · ${{esc(a.score.reason)}}</p></article>`).join('');root.insertAdjacentHTML('beforeend',`<section class="panel" id="${{esc(e.id)}}"><p class="eyebrow">${{esc(e.topic)}} · ${{esc(e.status)}}</p><h2>${{esc(e.title)}}</h2><p>${{esc(e.summary)}}</p><p><span class="badge">AI 判断 · ${{esc(e.analysis.confidence)}}</span> ${{esc(e.analysis.statement)}}</p><p class="meta">边界：${{esc(e.analysis.counterpoint)}}</p><details><summary>查看 ${{e.article_count}} 条事实依据</summary><div class="evidence">${{evidence}}</div></details></section>`);}});if(matches.length>visible.length)root.insertAdjacentHTML('beforeend',`<section class="panel">当前展示前 ${{visible.length}} 个事件，请使用搜索进一步筛选。</section>`);if(!root.children.length)root.innerHTML='<section class="panel">没有匹配的事件。</section>';}};document.getElementById('query').addEventListener('input',render);render();if(location.hash)requestAnimationFrame(()=>document.getElementById(location.hash.slice(1))?.scrollIntoView({{block:'start'}}));}}).catch(()=>document.getElementById('events').innerHTML='<section class="panel">事件数据暂不可用。</section>');
    </script></body></html>"""


def _search_html() -> str:
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>NewsRadar 全文搜索</title><link rel="icon" href="/assets/brand/favicon-32.png"><style>{_base_style()}</style></head><body><main class="shell"><header class="mast"><h1>NEWS SEARCH</h1><nav class="nav"><a class="button" href="/">首页</a><a class="button" href="/archive.html">历史</a><a class="button" href="/events.html">事件</a><a class="button" href="/trends.html">趋势</a></nav></header><input class="search" id="query" type="search" placeholder="搜索公司、产品、主题、来源或新闻标题" aria-label="搜索历史新闻"><div id="summary" class="panel">输入关键词开始搜索。</div><div id="results"></div></main><script>
    const esc=v=>String(v??'').replace(/[&<>\"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}}[c]));fetch('/search-index.json',{{cache:'no-store'}}).then(r=>r.json()).then(data=>{{const input=document.getElementById('query'),root=document.getElementById('results'),summary=document.getElementById('summary');const params=new URLSearchParams(location.search);input.value=params.get('q')||'';const render=()=>{{const q=input.value.trim().toLowerCase();root.innerHTML='';if(!q){{summary.textContent='输入关键词开始搜索。';return;}}const matches=data.articles.filter(a=>[a.title,a.source,...a.entities,...a.tags].join(' ').toLowerCase().includes(q)).sort((a,b)=>b.date.localeCompare(a.date)||b.score.total-a.score.total).slice(0,200);summary.textContent=`找到 ${{matches.length}} 条结果`;matches.forEach(a=>root.insertAdjacentHTML('beforeend',`<article class="panel"><p class="eyebrow">${{esc(a.date)}} · ${{esc(a.source)}} · 评分 ${{a.score.total}}</p><h2><a href="${{esc(a.url||a.report_url)}}" target="_blank" rel="noopener noreferrer">${{esc(a.title)}}</a></h2><p>${{esc(a.score.reason)}}</p>${{a.event_id?`<a href="/events.html#${{esc(a.event_id)}}">查看相关事件</a>`:''}}</article>`));}};input.addEventListener('input',render);render();}}).catch(()=>document.getElementById('summary').textContent='搜索索引暂不可用。');
    </script></body></html>"""


def write_outputs(site_dir: Path, payload: dict[str, Any]) -> None:
    site_dir.mkdir(parents=True, exist_ok=True)
    search_index = payload.get("search_index", [])
    public_payload = {key: value for key, value in payload.items() if key != "search_index"}
    site_dir.joinpath("experience.json").write_text(
        json.dumps(public_payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    site_dir.joinpath("search-index.json").write_text(
        json.dumps({"schema_version": 1, "articles": search_index}, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    site_dir.joinpath("notification-preview.json").write_text(
        json.dumps(payload["notification_preview"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    site_dir.joinpath("events.html").write_text(_events_html(), encoding="utf-8")
    site_dir.joinpath("search.html").write_text(_search_html(), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-dir", type=Path, default=REPO_DIR)
    parser.add_argument("--site-dir", type=Path, default=DEFAULT_SITE_DIR)
    args = parser.parse_args()
    payload = build_experience(args.repo_dir.resolve())
    write_outputs(args.site_dir.resolve(), payload)
    print(
        "[experience] generated: "
        f"{payload['coverage']['article_count']} articles, "
        f"{payload['coverage']['event_count']} events, "
        f"{payload['notification_preview']['candidate_count']} notification candidates"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
