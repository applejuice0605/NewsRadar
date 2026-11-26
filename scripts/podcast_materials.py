"""Select dated, substantive source material for the daily podcast.

This module is deliberately conservative.  It treats RSS summaries and fetched
pages as source text, never as independently verified facts, and refuses to
turn the database crawl date into a publication date.
"""
from __future__ import annotations

import hashlib
import importlib.util
import html
import ipaddress
import json
import math
import re
import time
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

POLICY_VERSION = "podcast-materials-v1"
MAX_TEXT_CHARS = 4000
MAX_ITEMS = 6
MAX_FETCH = 20
MAX_BUDGET_SECONDS = 60.0
CACHE_TTL_SECONDS = 6 * 60 * 60
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
COMMUNITY_HOSTS = {
    "juejin.cn", "v2ex.com", "zhihu.com", "weibo.com", "x.com", "twitter.com",
    "reddit.com", "csdn.net", "bilibili.com", "youtube.com", "douban.com",
    "segmentfault.com",
}
_BOILERPLATE = re.compile(
    r"登录|注册|订阅|cookie|javascript|enable cookies|privacy policy|terms of service|"
    r"目录|导航|上一页|下一页|阅读原文|扫码|版权所有|copyright",
    re.I,
)
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


def _strip_html(value: Any, *, allow_plain: bool = True) -> str:
    if not isinstance(value, str):
        return ""
    if allow_plain and "<" not in value:
        return re.sub(r"\s+", " ", html.unescape(value)).strip()
    parser = _ContentParser()
    parser.feed(value)
    parser.close()
    text = html.unescape(" ".join(parser.parts))
    if not text.strip() and allow_plain:
        # RSS fragments often contain only <p>; strip non-content blocks before
        # the small fallback so script/JSON-LD can never become spoken text.
        safe = re.sub(r"<(script|style|noscript)\b[^>]*>.*?</\1>", " ", value, flags=re.I | re.S)
        text = re.sub(r"<[^>]+>", " ", html.unescape(safe))
    return re.sub(r"\s+", " ", text).strip()


class _ContentParser(HTMLParser):
    """Extract paragraph-like text only from article/main content."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._scope_stack: list[tuple[str, bool]] = []
        self._skip_stack: list[tuple[str, bool]] = []
        self.published_values: list[str] = []
        self._jsonld_active = False
        self._jsonld_parts: list[str] = []
        self._pubtime_active = False
        self._pubtime_parts: list[str] = []

    @property
    def in_scope(self) -> bool:
        return any(item[1] for item in self._scope_stack)

    @property
    def skipped(self) -> bool:
        return any(item[1] for item in self._skip_stack)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = dict(attrs)
        classes = set((attrs_map.get("class") or "").lower().split())
        ident = (attrs_map.get("id") or "").lower()
        scope = (tag in {"article", "main"}
                 or bool(classes & {"article", "entry-content", "post-content", "post_content", "article-body", "article-content"})
                 or ident in {"article", "content", "main-content", "entry-content", "post-content"})
        skip = (tag in {"script", "style", "noscript", "nav", "footer", "header", "form", "aside"}
                or bool(classes & {"ad-tips", "source", "share", "related", "related-articles"}))
        inherited_scope = self._scope_stack[-1][1] if self._scope_stack else False
        inherited_skip = self._skip_stack[-1][1] if self._skip_stack else False
        name = (attrs_map.get("property") or attrs_map.get("name") or "").lower()
        if tag == "meta" and name in {"article:published_time", "datepublished"}:
            if attrs_map.get("content"):
                self.published_values.append(str(attrs_map["content"]))
        if tag == "time" and attrs_map.get("datetime") and (scope or inherited_scope):
            self.published_values.append(str(attrs_map["datetime"]))
        if tag == "script" and "ld+json" in (attrs_map.get("type") or "").lower():
            self._jsonld_active = True
            self._jsonld_parts = []
        if tag == "span" and ident == "pubtime_baidu":
            self._pubtime_active = True
            self._pubtime_parts = []
        if tag not in _VOID_TAGS:
            self._scope_stack.append((tag, scope or inherited_scope))
            self._skip_stack.append((tag, skip or inherited_skip))
        if not self.in_scope or self.skipped:
            return
        if tag in {"p", "h1", "h2", "h3", "h4", "li", "br"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if self.in_scope and not self.skipped and tag in {"p", "h1", "h2", "h3", "h4", "li"}:
            self.parts.append(" ")
        if tag == "script" and self._jsonld_active:
            self._jsonld_active = False
            if self._jsonld_parts:
                try:
                    data = json.loads("".join(self._jsonld_parts))
                except (TypeError, ValueError, json.JSONDecodeError):
                    data = None
                self._collect_jsonld_dates(data)
                self._jsonld_parts = []
        if tag == "span" and self._pubtime_active:
            self._pubtime_active = False
            value = "".join(self._pubtime_parts).strip()
            if value:
                self.published_values.append(value)
            self._pubtime_parts = []
        for stack in (self._scope_stack, self._skip_stack):
            for index in range(len(stack) - 1, -1, -1):
                if stack[index][0] == tag:
                    del stack[index:]
                    break

    def handle_data(self, data: str) -> None:
        if self._jsonld_active:
            self._jsonld_parts.append(data)
        if self._pubtime_active:
            self._pubtime_parts.append(data)
        if self.in_scope and not self.skipped:
            self.parts.append(data)

    def _collect_jsonld_dates(self, value: Any, in_article: bool = False) -> None:
        if isinstance(value, dict):
            raw_type = value.get("@type")
            types = {str(item).casefold() for item in raw_type} if isinstance(raw_type, list) else {str(raw_type).casefold()}
            article_here = in_article or bool(types & {"article", "newsarticle", "reportagenewsarticle", "blogposting", "liveblogposting"})
            for key, item in value.items():
                if key == "datePublished" and article_here and isinstance(item, str):
                    self.published_values.append(item)
                elif key != "dateModified":
                    self._collect_jsonld_dates(item, article_here or key == "mainArticle")
        elif isinstance(value, list):
            for item in value:
                self._collect_jsonld_dates(item, in_article)


def _extract_page(raw: str) -> tuple[str, date | None]:
    parser = _ContentParser()
    parser.feed(raw)
    parser.close()
    text = re.sub(r"\s+", " ", html.unescape(" ".join(parser.parts))).strip()
    published = next((_date_value(value) for value in parser.published_values if _date_value(value)), None)
    return text, published


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        parsed = None
        for pattern in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d"):
            try:
                parsed = datetime.strptime(value.strip(), pattern)
                break
            except ValueError:
                continue
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TZ)
    return parsed.astimezone(LOCAL_TZ)


def _date_value(value: Any) -> date | None:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(LOCAL_TZ).date()
        return value.date()
    if isinstance(value, date):
        return value
    parsed = _parse_datetime(value)
    if parsed:
        return parsed.date()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def _summary_ok(title: str, summary: Any) -> tuple[bool, str]:
    text = _strip_html(summary)
    title_text = _strip_html(title)
    if not text:
        return False, "summary_empty"
    normalized_text = re.sub(r"[\W_]+", "", text, flags=re.UNICODE).lower()
    normalized_title = re.sub(r"[\W_]+", "", title_text, flags=re.UNICODE).lower()
    if (text == title_text or text.rstrip("。.!！") == title_text.rstrip("。.!！")
            or (normalized_title and (normalized_text == normalized_title
                                      or normalized_text.startswith(normalized_title)
                                      and len(normalized_text) - len(normalized_title) < 24))):
        return False, "summary_repeats_title"
    if _BOILERPLATE.search(text) and (len(text) < 220 or len(re.findall(r"登录|注册|导航|上一页|下一页|阅读原文", text)) >= 2):
        return False, "summary_boilerplate"
    cjk_chars = re.findall(r"[\u3400-\u9fff]", text)
    latin_words = re.findall(r"[A-Za-z]+(?:['-][A-Za-z]+)?", text)
    if len(cjk_chars) < 80 and len(latin_words) < 40:
        return False, "summary_too_short"
    return True, "summary_substantive"


def _fresh_date(published: date | None, report_date: date, max_age_days: int) -> tuple[bool, str]:
    if published is None:
        return False, "published_at_missing"
    if published > report_date:
        return False, "published_at_future"
    if published < report_date - timedelta(days=max_age_days):
        return False, "published_at_too_old"
    return True, "published_at_fresh"


def _item_id(article: dict[str, Any], text: str) -> str:
    raw = "\x1f".join(str(article.get(key) or "") for key in ("url", "title", "source")) + "\x1f" + text
    return "podcast_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:18]


def _safe_relevance(article: dict[str, Any]) -> float:
    tags = article.get("tags")
    if not isinstance(tags, list):
        return 0.0
    values: list[float] = []
    for item in tags:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            value = float(item[1])
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            values.append(value)
    return max(values, default=0.0)


def _numeric_signal(article: dict[str, Any], *names: str) -> float:
    """Read an existing hotness/importance signal without inventing one."""
    for name in names:
        value = article.get(name)
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            return number
    return 0.0


def _selection_key(article: dict[str, Any]) -> tuple[float, float, float, float, float, float]:
    # Lower current/best rank is hotter. Relevance is only a deterministic
    # tie-breaker and never the heat score used to choose a story.
    rank = _numeric_signal(article, "rank")
    if rank <= 0:
        rank = _numeric_signal(article, "best_rank")
    has_rank = rank > 0
    hotness = _numeric_signal(article, "hotness", "importance", "frequency", "count")
    published = _date_value(article.get("published_at")) or date.min
    # Community posts are supplementary context and must not displace
    # available source-report positions in the core selection.
    community = 1.0 if _source_genre(article) == "community_post" else 0.0
    return (community, 0.0 if has_rank else 1.0, rank if has_rank else 0.0,
            -hotness, -published.toordinal(), -_safe_relevance(article))


def _normalize_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    try:
        parsed = urlsplit(value.strip())
    except (TypeError, ValueError):
        return value.strip().casefold()
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip('/') or '/'}?{parsed.query}".rstrip("?")


def _normalize_title(value: Any) -> str:
    return re.sub(r"[^\w\u3400-\u9fff]+", "", str(value or "").casefold())


def _source_genre(article: dict[str, Any]) -> str:
    """Classify publisher context; this is not a truth/quality judgment."""
    explicit = article.get("source_genre")
    if explicit in {"community_post", "source_report"}:
        return str(explicit)
    try:
        host = (urlsplit(str(article.get("url") or "")).hostname or "").lower().removeprefix("www.")
    except ValueError:
        host = ""
    source_id = str(article.get("source_id") or "").casefold()
    is_community = any(host == domain or host.endswith("." + domain) for domain in COMMUNITY_HOSTS)
    is_community = is_community or source_id.startswith("community:")
    return "community_post" if is_community else "source_report"


def _safe_url(value: Any) -> bool:
    """Reject malformed, private, credential-bearing, or non-HTTPS URLs."""
    if not isinstance(value, str) or not value or value != value.strip() or any(ord(ch) < 32 for ch in value):
        return False
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return False
    if parsed.scheme.lower() != "https" or not hostname or parsed.username or parsed.password or port not in (None, 443):
        return False
    host = hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return ip.is_global


def _same_article_url(original: str, actual: str) -> bool:
    if not _safe_url(actual):
        return False
    try:
        first, second = urlsplit(original), urlsplit(actual)
        host_a = (first.hostname or "").lower().removeprefix("www.")
        host_b = (second.hostname or "").lower().removeprefix("www.")
        path_a = first.path.rstrip("/") or "/"
        path_b = second.path.rstrip("/") or "/"
        return host_a == host_b and path_a == path_b
    except (TypeError, ValueError):
        return False


def _candidate_payload(article: dict[str, Any], text: str, published: date, material_kind: str, scope: str) -> dict[str, Any]:
    return {
        "id": _item_id(article, text),
        "title": str(article.get("title") or "").strip(),
        "source": str(article.get("source") or article.get("source_name") or "").strip(),
        "url": str(article.get("url") or "").strip(),
        "published_at": published.isoformat(),
        "text": text[:MAX_TEXT_CHARS],
        "material_kind": material_kind,
        "evidence_scope": scope,
        "source_genre": _source_genre(article),
        "source_truth_verified": False,
    }


def _default_candidates(repo_dir: Path) -> list[dict[str, Any]]:
    from generate_site_insights import load_articles
    articles, _ = load_articles(repo_dir / "TrendRadar" / "output")
    return articles


def _is_candidate(article: dict[str, Any], report_date: date) -> tuple[bool, str]:
    if _date_value(article.get("date")) != report_date:
        return False, "crawl_date_mismatch"
    source_id = str(article.get("source_id") or "")
    article_key = str(article.get("key") or article.get("id") or "")
    if not (source_id.startswith("rss:") or source_id.startswith("news:") or ":news:" in article_key):
        return False, "not_news_material"
    try:
        from generate_site_insights import peripheral_reason
        if peripheral_reason(str(article.get("title") or "")):
            return False, "peripheral_topic"
    except (ImportError, AttributeError):
        # Unit use outside the project does not need the optional quality module.
        pass
    url = str(article.get("url") or "").strip()
    if not url:
        return False, "url_missing"
    if not _safe_url(url):
        return False, "url_unsafe"
    tags = article.get("tags")
    if not isinstance(tags, list) or not tags:
        return False, "no_quality_tag"
    if _safe_relevance(article) < 0.7:
        return False, "quality_below_threshold"
    # Same title-evidence gate as the recommendation list, before fetching
    # source bodies. Raw collection and explicit historical input stay intact.
    spec = importlib.util.spec_from_file_location("podcast_news_quality", Path(__file__).resolve().parents[1] / "TrendRadar/trendradar/ai/news_quality.py")
    quality = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(quality)
    if not quality.title_evidence_eligible(article.get("title", ""), article.get("assessment")):
        return False, "title_evidence_not_qualified"
    return True, "candidate"


class _SafeFetcher:
    """Lazy, bounded source-page reader using PrimaryEnricher's network policy."""

    def __init__(self, cache_dir: Path, max_fetch: int, budget_seconds: float, reader: Any = None) -> None:
        self.cache_dir = Path(cache_dir) / "source-pages"
        self.max_fetch = max(0, min(int(max_fetch), MAX_FETCH))
        self.budget_seconds = max(0.0, min(float(budget_seconds), 60.0))
        self._reader = reader
        self._enricher = None

    def supports(self, url: str) -> bool:
        if self._reader is not None:
            return True
        try:
            from podcast_source_reader import allowed_url
        except ImportError:
            return False
        return bool(allowed_url(url, publisher=True))

    def _get_enricher(self):
        if self._enricher is None and self._reader is None:
            try:
                from podcast_source_reader import PrimaryEnricher
            except ImportError:
                return None
            self._enricher = PrimaryEnricher(
                self.cache_dir / "primary",
                max_articles=self.max_fetch,
                max_requests=self.max_fetch * 3,
                budget_seconds=self.budget_seconds,
            )
        return self._enricher

    def __call__(self, article: dict[str, Any]) -> dict[str, Any] | None:
        url = str(article.get("url") or "").strip()
        if not url:
            return None
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        cache_file = self.cache_dir / f"{key}.json"
        now = time.time()
        try:
            saved = json.loads(cache_file.read_text(encoding="utf-8"))
            if now - float(saved.get("saved_at", 0)) < CACHE_TTL_SECONDS:
                cached = saved.get("result")
                if (isinstance(cached, dict) and cached.get("text") and cached.get("published_at")
                        and _same_article_url(url, str(cached.get("url") or ""))):
                    return cached
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
        reader = self._reader
        enricher = self._get_enricher()
        if reader is None:
            if enricher is None:
                return None
            from podcast_source_reader import allowed_url
            if not allowed_url(url, publisher=True):
                return None
            # Robots are checked before the raw read, and no link discovery is done.
            if not enricher._robot_allowed(url, publisher=True):
                return None
            try:
                actual, raw = enricher._read(url, publisher=True)
            except Exception:
                return None
        else:
            try:
                actual, raw = reader(url)
            except Exception:
                return None
        if not _same_article_url(url, actual):
            return None
        text, published = _extract_page(raw)
        if not text or published is None:
            return None
        result = {"text": text[:MAX_TEXT_CHARS], "published_at": published.isoformat(), "url": actual}
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps({"saved_at": now, "result": result}, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return result


def validate_payload(payload: Any, report_date: date) -> tuple[bool, list[str]]:
    """Validate the hand-off contract; an empty item list is never model input."""
    errors: list[str] = []
    if not isinstance(payload, dict):
        return False, ["payload_not_object"]
    if type(payload.get("schema_version")) is not int or payload.get("schema_version") != 1 or payload.get("policy_version") != POLICY_VERSION:
        errors.append("schema_mismatch")
    if payload.get("date") != report_date.isoformat():
        errors.append("report_date_mismatch")
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        errors.append("items_empty")
        return False, errors
    if len(items) > 6:
        errors.append("items_over_limit")
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            errors.append("item_not_object")
            continue
        url = item.get("url")
        if not _safe_url(url):
            errors.append("unsafe_url")
        norm_url, norm_title = _normalize_url(url), _normalize_title(item.get("title"))
        if norm_url in seen_urls or norm_title in seen_titles:
            errors.append("duplicate_item")
        seen_urls.add(norm_url)
        seen_titles.add(norm_title)
        published = _date_value(item.get("published_at"))
        fresh, reason = _fresh_date(published, report_date, 3)
        if not fresh:
            errors.append(reason)
        if not item.get("id") or not item.get("title") or not item.get("source"):
            errors.append("item_fields_missing")
        if not isinstance(item.get("text"), str) or len(item.get("text", "")) > MAX_TEXT_CHARS:
            errors.append("text_over_limit")
        if not _summary_ok(str(item.get("title") or ""), item.get("text"))[0]:
            errors.append("text_not_substantive")
    return not errors, errors


def build_materials(
    repo_dir: Path,
    report_date: date,
    cache_dir: Path,
    *,
    candidates: list[dict[str, Any]] | None = None,
    fetcher: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
    max_items: int = MAX_ITEMS,
    max_fetch: int = MAX_FETCH,
    max_age_days: int = 3,
    budget_seconds: float = MAX_BUDGET_SECONDS,
    fetch_enabled: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return only dated source text suitable for one editorial writing call."""
    started = time.monotonic()
    item_limit = max(0, min(int(max_items), MAX_ITEMS))
    fetch_limit = max(0, min(int(max_fetch), MAX_FETCH))
    budget_limit = max(0.0, min(float(budget_seconds), MAX_BUDGET_SECONDS))
    candidates = list(candidates) if candidates is not None else _default_candidates(Path(repo_dir))
    candidates.sort(key=_selection_key)
    if fetcher is None and fetch_enabled:
        fetcher = _SafeFetcher(Path(cache_dir), fetch_limit, budget_limit)
    report: dict[str, Any] = {"policy_version": POLICY_VERSION, "items": [], "rejected": [], "fetch_attempts": 0}
    items: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    community_count = 0
    for article in candidates:
        if len(items) >= item_limit:
            report["rejected"].append({"title": str(article.get("title") or ""), "reason": "max_items"})
            continue
        ok, reason = _is_candidate(article, report_date)
        if not ok:
            report["rejected"].append({"title": str(article.get("title") or ""), "reason": reason})
            continue
        genre = _source_genre(article)
        if genre == "community_post" and community_count >= 1:
            report["rejected"].append({"title": str(article.get("title") or ""), "reason": "community_limit"})
            continue
        title = str(article.get("title") or "").strip()
        pub = _date_value(article.get("published_at"))
        date_ok, date_reason = _fresh_date(pub, report_date, max_age_days)
        if pub is not None and not date_ok:
            report["rejected"].append({"title": title, "reason": date_reason})
            continue
        valid, text_reason = _summary_ok(title, article.get("summary"))
        text = _strip_html(article.get("summary"))
        kind, scope = "rss_summary", "source_summary"
        if not (date_ok and valid):
            if pub is not None and not date_ok:
                report["rejected"].append({"title": title, "reason": date_reason})
                continue
            if fetcher is None or report["fetch_attempts"] >= fetch_limit or time.monotonic() - started >= budget_limit:
                report["rejected"].append({"title": title, "reason": text_reason if not valid else date_reason})
                continue
            if isinstance(fetcher, _SafeFetcher) and not fetcher.supports(str(article.get("url") or "")):
                report["rejected"].append({"title": title, "reason": "source_fetch_unsupported"})
                continue
            report["fetch_attempts"] += 1
            try:
                fetched = fetcher(article) or {}
            except Exception as exc:
                report["rejected"].append({"title": title, "reason": "fetch_failed", "error": type(exc).__name__})
                continue
            if time.monotonic() - started >= budget_limit:
                report["rejected"].append({"title": title, "reason": "fetch_budget_exhausted"})
                continue
            fetched_text = _strip_html(fetched.get("text") or fetched.get("summary"))
            fetched_date = _date_value(fetched.get("published_at") or fetched.get("datePublished"))
            fresh, fetched_reason = _fresh_date(fetched_date, report_date, max_age_days)
            if not fresh or not _summary_ok(title, fetched_text)[0]:
                report["rejected"].append({"title": title, "reason": fetched_reason if not fresh else "fetched_text_invalid"})
                continue
            text, pub, kind, scope = fetched_text, fetched_date, "source_page", "source_page_text"
        if pub is None:
            report["rejected"].append({"title": title, "reason": "published_at_missing"})
            continue
        url_key, title_key = _normalize_url(article.get("url")), _normalize_title(title)
        if url_key in seen_urls or title_key in seen_titles:
            report["rejected"].append({"title": title, "reason": "duplicate_source"})
            continue
        seen_urls.add(url_key)
        seen_titles.add(title_key)
        items.append(_candidate_payload(article, text, pub, kind, scope))
        if genre == "community_post":
            community_count += 1
    # Keep the candidate order established by _selection_key. The writer only
    # receives source text, so re-sorting by publication date here would erase
    # the platform-local rank/importance priority selected upstream.
    payload = {"schema_version": 1, "policy_version": POLICY_VERSION, "date": report_date.isoformat(), "items": items,
               "source_materials_verified": False}
    report["items"] = [{"id": item["id"], "title": item["title"], "reason": "accepted"} for item in items]
    report["accepted_count"] = len(items)
    report["candidate_count"] = len(candidates)
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    report["cache_dir"] = str(Path(cache_dir))
    report["validation"] = validate_payload(payload, report_date)
    return payload, report


__all__ = ["POLICY_VERSION", "build_materials", "validate_payload"]
