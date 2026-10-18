#!/usr/bin/env python3
"""Apply environment-specific feature visibility to a generated static site."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path


FEATURE_DEFAULTS = {
    "production": {"speech": False, "trends": False, "status": False, "sources": False, "experience": False},
    "preview": {"speech": True, "trends": True, "status": True, "sources": True, "experience": True},
}

ROUTE_FILES = {
    "trends": ("trends.html", "trends.json"),
    "status": ("status.html", "health.json"),
    "sources": ("sources.html", "sources.json"),
    "experience": ("experience.json", "search-index.json", "events.html", "search.html", "notification-preview.json"),
}

FAVICON_HTML = (
    '<link rel="icon" type="image/png" sizes="32x32" href="/assets/brand/favicon-32.png">'
    '<link rel="apple-touch-icon" sizes="180x180" href="/assets/brand/apple-touch-icon.png">'
)
POWERED_BY_HTML = (
    'Powered by <a href="https://github.com/applejuice0605/NewsRadar.git" '
    'target="_blank" rel="noopener noreferrer" class="footer-link project-name">NewsRadar</a>'
)
WORDMARK_HTML = (
    '<div class="header-brand" aria-label="NewsRadar">'
    '<img class="header-brand-mark" src="/assets/brand/newsradar-mark-512.png" alt="">'
    '<img class="header-brand-wordmark" src="/assets/brand/newsradar-wordmark.png" alt="NewsRadar">'
    '</div>'
)
WORDMARK_STYLE = (
    '<style id="newsradar-brand-style">'
    '.header-brand{display:block;width:clamp(300px,38vw,380px);margin:0;position:absolute;top:6px;left:12px;z-index:2}'
    '.header-brand-mark{position:absolute;top:-4px;right:0;width:clamp(56px,6vw,68px);height:auto;z-index:2}'
    '.header-brand-wordmark{display:block;width:calc(100% - 28px);height:auto}'
    '.header-title{margin-top:60px}'
    '@media(max-width:480px){.header-brand{width:min(286px,78vw);top:6px;left:8px}'
    '.header-brand-mark{width:52px;top:-3px}.header-brand-wordmark{width:calc(100% - 24px)}'
    '.save-buttons{margin-top:98px}.header-title{margin-top:12px}'
    '.header-info,body.wide-mode .header-info{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}'
    'body.wide-mode .container{max-width:100%}body.wide-mode .content{padding:16px}'
    'body.wide-mode .rss-feeds-grid,body.wide-mode .ai-section .ai-blocks-grid,'
    'body.wide-mode .new-section .new-sources-grid,'
    'body.wide-mode .standalone-section .standalone-groups-grid{grid-template-columns:1fr}}'
    '</style>'
)
SOCIAL_IMAGE_HTML = (
    '<meta property="og:image" content="https://newsradar.ccwu.cc/assets/brand/newsradar-share-1200x630.png">'
    '<meta property="og:image:width" content="1200">'
    '<meta property="og:image:height" content="630">'
)
SPEECH_ASSETS_HTML = (
    '<link rel="stylesheet" href="/assets/report-speech.css">'
    '<script src="/assets/report-speech.js" defer></script>'
)
EXPERIENCE_ASSETS_HTML = (
    '<link rel="stylesheet" href="/assets/report-experience.css">'
    '<script src="/assets/report-experience.js" defer></script>'
)
FEATURE_LINKS = {
    "trends": '<a class="site-archive-link" href="/trends.html" title="查看七天和三十天趋势">趋势</a>',
    "status": '<a class="site-archive-link" href="/status.html" title="查看采集与发布状态">状态</a>',
    "sources": '<a class="site-archive-link" href="/sources.html" title="查看候选源灰度内容">候选</a>',
    "experience": (
        '<a class="site-archive-link" href="/search.html" title="搜索历史新闻">搜索</a>'
        '<a class="site-archive-link" href="/events.html" title="查看事件时间线">事件</a>'
    ),
}


def parse_switch(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    raise ValueError(f"unsupported feature switch value: {value!r}")


def remove_anchor(content: str, route: str) -> str:
    pattern = rf'<a\b[^>]*\bhref=["\']/({re.escape(route)})(?:\.html)?["\'][^>]*>.*?</a>'
    return re.sub(pattern, "", content, flags=re.IGNORECASE | re.DOTALL)


def apply_features(site_dir: Path, environment: str, switches: dict[str, bool]) -> None:
    for html_path in site_dir.rglob("*.html"):
        content = html_path.read_text(encoding="utf-8")
        content = re.sub(
            r'<style id="newsradar-brand-style">.*?</style>',
            WORDMARK_STYLE,
            content,
            count=1,
            flags=re.IGNORECASE | re.DOTALL,
        )
        content = re.sub(
            r'<link rel="icon"[^>]*href="/assets/newsradar\.png">',
            '<link rel="icon" type="image/png" sizes="32x32" href="/assets/brand/favicon-32.png">',
            content,
            flags=re.IGNORECASE,
        )
        content = re.sub(
            r'<link rel="apple-touch-icon"[^>]*href="/assets/newsradar\.png">',
            '<link rel="apple-touch-icon" sizes="180x180" href="/assets/brand/apple-touch-icon.png">',
            content,
            flags=re.IGNORECASE,
        )
        content = re.sub(
            r'<meta property="og:image"[^>]*>',
            '<meta property="og:image" content="https://newsradar.ccwu.cc/assets/brand/newsradar-share-1200x630.png">',
            content,
            count=1,
            flags=re.IGNORECASE,
        )
        if "/assets/brand/favicon-32.png" not in content and "</head>" in content:
            content = content.replace("</head>", f"{FAVICON_HTML}</head>", 1)
        if 'property="og:image"' not in content and "</head>" in content:
            content = content.replace("</head>", f"{SOCIAL_IMAGE_HTML}</head>", 1)
        content = re.sub(
            r'<img class="header-brand"[^>]*>',
            WORDMARK_HTML,
            content,
            count=1,
            flags=re.IGNORECASE,
        )
        if 'class="header-title"' in content and 'class="header-brand"' not in content:
            content = content.replace("</head>", f"{WORDMARK_STYLE}</head>", 1)
            content = content.replace('<div class="header-title">', f'{WORDMARK_HTML}<div class="header-title">', 1)
        content = re.sub(
            r'由\s*<span class="project-name">TrendRadar</span>\s*生成\s*·\s*'
            r'<a href="https://github\.com/sansan0/TrendRadar"[^>]*>\s*GitHub 开源项目\s*</a>',
            POWERED_BY_HTML,
            content,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if switches["speech"]:
            if "/assets/report-speech.css" not in content and "</head>" in content:
                content = content.replace("</head>", f"{SPEECH_ASSETS_HTML}</head>", 1)
        else:
            content = re.sub(
                r'\s*<link\b[^>]*href=["\']/assets/report-speech\.css["\'][^>]*>',
                "",
                content,
                flags=re.IGNORECASE,
            )
            content = re.sub(
                r'\s*<script\b[^>]*src=["\']/assets/report-speech\.js["\'][^>]*>\s*</script>',
                "",
                content,
                flags=re.IGNORECASE,
            )
        if switches["experience"]:
            if "/assets/report-experience.css" not in content and "</head>" in content:
                content = content.replace("</head>", f"{EXPERIENCE_ASSETS_HTML}</head>", 1)
        else:
            content = re.sub(
                r'\s*<link\b[^>]*href=["\']/assets/report-experience\.css["\'][^>]*>',
                "",
                content,
                flags=re.IGNORECASE,
            )
            content = re.sub(
                r'\s*<script\b[^>]*src=["\']/assets/report-experience\.js["\'][^>]*>\s*</script>',
                "",
                content,
                flags=re.IGNORECASE,
            )
        enabled_links = "".join(
            FEATURE_LINKS[feature]
            for feature in ("trends", "status", "sources", "experience")
            if switches[feature]
            and (
                (feature != "experience" and f'/{feature}.html' not in content)
                or (feature == "experience" and '/events.html' not in content)
            )
        )
        if enabled_links and 'href="/archive.html"' in content and 'class="header-title"' in content:
            archive_anchor = re.search(r'<a\b[^>]*href="/archive\.html"[^>]*>.*?</a>', content, re.DOTALL)
            if archive_anchor:
                content = content[: archive_anchor.end()] + enabled_links + content[archive_anchor.end() :]
        for feature in ROUTE_FILES:
            if not switches[feature]:
                content = remove_anchor(content, feature)
        if not switches["experience"]:
            content = remove_anchor(remove_anchor(content, "events"), "search")
        html_path.write_text(content, encoding="utf-8")

    for feature, filenames in ROUTE_FILES.items():
        if switches[feature]:
            continue
        for filename in filenames:
            site_dir.joinpath(filename).unlink(missing_ok=True)

    if not switches["speech"]:
        shutil.rmtree(site_dir / "audio", ignore_errors=True)

    manifest = {"environment": environment, "features": switches}
    site_dir.joinpath("site-features.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-dir", type=Path, required=True)
    parser.add_argument("--environment", choices=tuple(FEATURE_DEFAULTS), default="production")
    for feature in FEATURE_DEFAULTS["production"]:
        parser.add_argument(f"--{feature}")
    args = parser.parse_args()

    defaults = FEATURE_DEFAULTS[args.environment]
    switches = {
        feature: parse_switch(getattr(args, feature), defaults[feature])
        for feature in defaults
    }
    apply_features(args.site_dir.resolve(), args.environment, switches)
    enabled = ", ".join(name for name, enabled in switches.items() if enabled) or "none"
    print(f"[site] environment={args.environment}; enabled features: {enabled}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
