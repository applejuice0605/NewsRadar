#!/usr/bin/env python3
"""Generate the public TrendRadar archive page."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from html import escape


REPO_DIR = Path(__file__).resolve().parents[1]
SITE_DIR = REPO_DIR / "docs"
REPORTS_DIR = SITE_DIR / "reports"
ARCHIVE_FILE = SITE_DIR / "archive.html"


@lru_cache(maxsize=None)
def _report_profile(rel_path_text: str) -> tuple[bool, bool, bool]:
    """Return (has_ai_daily, is_daily, has_ai) for a generated report."""
    path = SITE_DIR / rel_path_text
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return (False, False, False)

    is_daily = "全天汇总" in content
    has_ai = '<div class="ai-section-title"' in content and "AI 热点分析" in content
    return (is_daily and has_ai, is_daily, has_ai)


def _report_sort_key(path: Path) -> tuple[bool, bool, bool, str]:
    has_ai_daily, is_daily, has_ai = _report_profile(str(path))
    return (has_ai_daily, is_daily, has_ai, str(path))


def _report_dates() -> dict[str, list[Path]]:
    grouped: dict[str, list[Path]] = defaultdict(list)
    if not REPORTS_DIR.exists():
        return grouped

    for path in REPORTS_DIR.rglob("*.html"):
        rel = path.relative_to(SITE_DIR)
        parts = rel.parts
        if len(parts) >= 3 and parts[0] == "reports" and _is_date(parts[1]):
            grouped[parts[1]].append(rel)
        elif len(parts) == 2 and parts[0] == "reports":
            date_key = path.stem
            if _is_date(date_key):
                grouped[date_key].append(rel)

    for paths in grouped.values():
        paths.sort(key=lambda p: str(p), reverse=True)
    return dict(sorted(grouped.items(), reverse=True))


def _is_date(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _page_html(grouped: dict[str, list[Path]]) -> str:
    dates = list(grouped.keys())
    min_date = min(dates) if dates else ""
    max_date = max(dates) if dates else ""
    dates_js = ", ".join(f'"{escape(date)}"' for date in dates)
    cards = []
    for date, paths in grouped.items():
        preferred = max(paths, key=_report_sort_key) if paths else ""
        has_ai_daily, is_daily, _has_ai = _report_profile(str(preferred)) if preferred else (False, False, False)
        report_label = "AI 汇总" if has_ai_daily else "当天汇总" if is_daily else "当天快照"
        badge = '<span class="ai-badge">AI 分析</span>' if has_ai_daily else ""
        links = "\n".join(
            f'<li><a href="/{escape(str(path))}">{escape(path.stem)}</a></li>'
            for path in paths[:12]
        )
        cards.append(
            f"""
            <section class="archive-card" data-date="{escape(date)}">
                <div>
                    <h2>{escape(date)}{badge}</h2>
                    <p>{len(paths)} 份报告</p>
                </div>
                <a class="primary-link" href="/{escape(str(preferred))}">查看 {report_label}</a>
                <details>
                    <summary>展开当天所有快照</summary>
                    <ul>{links}</ul>
                </details>
            </section>
            """.strip()
        )

    cards_html = "\n".join(cards) or '<p class="empty">暂无历史报告</p>'

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TrendRadar 历史报告</title>
  <style>
    :root {{
      --ink: #1a1a1a;
      --paper: #fffef0;
      --red: #ff3333;
      --yellow: #ffcc00;
      --blue: #3366ff;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 24px;
      background: var(--paper);
      background-image: radial-gradient(circle, rgba(26,26,26,.18) 1px, transparent 1px);
      background-size: 4px 4px;
      color: var(--ink);
      font-family: "Arial Black", Impact, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-weight: 900;
    }}
    .shell {{ max-width: 960px; margin: 0 auto; }}
    header {{
      background: var(--red);
      border: 4px solid var(--ink);
      box-shadow: 10px 10px 0 var(--ink);
      padding: 22px;
      margin-bottom: 24px;
    }}
    h1 {{ margin: 0 0 12px; font-size: clamp(28px, 7vw, 58px); text-transform: uppercase; }}
    .toolbar {{ display: flex; flex-wrap: wrap; gap: 12px; align-items: center; }}
    select, input, a.button, button.button, .primary-link {{
      background: white;
      color: var(--ink);
      border: 4px solid var(--ink);
      box-shadow: 4px 4px 0 var(--ink);
      padding: 10px 12px;
      font: inherit;
      cursor: pointer;
      text-decoration: none;
      min-height: 44px;
    }}
    a.button:hover, button.button:hover, .primary-link:hover {{
      background: var(--yellow);
      transform: scale(1.04) rotate(-2deg);
    }}
    .archive-card {{
      background: white;
      border: 4px solid var(--ink);
      box-shadow: 6px 6px 0 var(--ink);
      padding: 18px;
      margin-bottom: 18px;
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 14px;
      align-items: start;
    }}
    h2 {{ margin: 0; font-size: 24px; }}
    .ai-badge {{
      display: inline-block;
      margin-left: 10px;
      padding: 4px 7px;
      background: var(--blue);
      color: white;
      border: 3px solid var(--ink);
      box-shadow: 3px 3px 0 var(--ink);
      font-size: 12px;
      vertical-align: middle;
    }}
    p {{ margin: 8px 0 0; color: #4a4a4a; }}
    details {{ grid-column: 1 / -1; }}
    summary {{ cursor: pointer; }}
    ul {{ margin: 12px 0 0; padding-left: 22px; line-height: 1.9; }}
    li a {{ color: var(--blue); text-decoration-thickness: 3px; }}
    .empty {{ background: white; border: 4px solid var(--ink); padding: 18px; }}
    .calendar {{
      background: white;
      border: 4px solid var(--ink);
      box-shadow: 6px 6px 0 var(--ink);
      padding: 16px;
      margin-bottom: 20px;
    }}
    .calendar-head {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin-bottom: 12px;
    }}
    .calendar-head button {{
      background: var(--yellow);
      border: 4px solid var(--ink);
      box-shadow: 3px 3px 0 var(--ink);
      min-width: 44px;
      min-height: 44px;
      font: inherit;
      cursor: pointer;
    }}
    .calendar-title {{ font-size: 20px; text-align: center; }}
    .calendar-grid {{
      display: grid;
      grid-template-columns: repeat(7, minmax(0, 1fr));
      gap: 8px;
    }}
    .weekday {{
      text-align: center;
      font-size: 12px;
      color: #4a4a4a;
    }}
    .day {{
      min-height: 44px;
      background: #f6f6f6;
      border: 3px solid var(--ink);
      color: #6b6b6b;
      font: inherit;
      cursor: default;
    }}
    .day.has-data {{
      background: var(--yellow);
      color: var(--ink);
      cursor: pointer;
      box-shadow: 3px 3px 0 var(--ink);
    }}
    .day.selected {{
      background: var(--blue);
      color: white;
    }}
    .day.blank {{
      visibility: hidden;
    }}
    .no-result {{
      display: none;
      background: white;
      border: 4px solid var(--ink);
      box-shadow: 4px 4px 0 var(--ink);
      padding: 16px;
      margin-bottom: 18px;
    }}
    @media (max-width: 640px) {{
      body {{ padding: 14px; }}
      .archive-card {{ grid-template-columns: 1fr; }}
      .toolbar label {{ width: 100%; }}
      input[type="date"] {{ width: 100%; }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <header>
      <h1>TrendRadar Archive</h1>
      <div class="toolbar">
        <a class="button" href="/">返回首页</a>
        <a class="button" href="/trends.html">趋势查询</a>
        <a class="button" href="/events.html">事件追踪</a>
        <a class="button" href="/search.html">全文搜索</a>
        <a class="button" href="/status.html">运行状态</a>
        <a class="button" href="/sources.html">候选源</a>
        <button class="button" type="button" id="showAll">全部日期</button>
        <button class="button" type="button" id="showLatest">最新日期</button>
        <label>
          选择日期
          <input type="date" id="dateInput" min="{escape(min_date)}" max="{escape(max_date)}">
        </label>
      </div>
    </header>
    <section class="calendar" aria-label="报告日期日历">
      <div class="calendar-head">
        <button type="button" id="prevMonth" aria-label="上个月">&lt;</button>
        <div class="calendar-title" id="calendarTitle"></div>
        <button type="button" id="nextMonth" aria-label="下个月">&gt;</button>
      </div>
      <div class="calendar-grid" id="calendarGrid"></div>
    </section>
    <div class="no-result" id="noResult">这一天暂无报告。黄色日期代表有历史数据。</div>
    <div id="archiveList">{cards_html}</div>
  </main>
  <script>
    const availableDates = new Set([{dates_js}]);
    const sortedDates = Array.from(availableDates).sort();
    const input = document.getElementById('dateInput');
    const cards = Array.from(document.querySelectorAll('.archive-card'));
    const noResult = document.getElementById('noResult');
    const calendarTitle = document.getElementById('calendarTitle');
    const calendarGrid = document.getElementById('calendarGrid');
    const labels = ['一', '二', '三', '四', '五', '六', '日'];
    let selectedDate = '';
    let visibleMonth = sortedDates.length ? new Date(sortedDates[sortedDates.length - 1] + 'T00:00:00') : new Date();

    function filterByDate(value) {{
      selectedDate = value || '';
      if (input.value !== selectedDate) input.value = selectedDate;
      let visibleCount = 0;
      cards.forEach(card => {{
        const show = !selectedDate || card.dataset.date === selectedDate;
        card.style.display = show ? '' : 'none';
        if (show) visibleCount++;
      }});
      noResult.style.display = selectedDate && visibleCount === 0 ? 'block' : 'none';
      renderCalendar();
    }}

    function renderCalendar() {{
      calendarGrid.innerHTML = '';
      labels.forEach(label => {{
        const node = document.createElement('div');
        node.className = 'weekday';
        node.textContent = label;
        calendarGrid.appendChild(node);
      }});

      const year = visibleMonth.getFullYear();
      const month = visibleMonth.getMonth();
      calendarTitle.textContent = `${{year}}-${{String(month + 1).padStart(2, '0')}}`;

      const first = new Date(year, month, 1);
      const firstOffset = (first.getDay() + 6) % 7;
      const daysInMonth = new Date(year, month + 1, 0).getDate();

      for (let i = 0; i < firstOffset; i++) {{
        const blank = document.createElement('div');
        blank.className = 'day blank';
        calendarGrid.appendChild(blank);
      }}

      for (let day = 1; day <= daysInMonth; day++) {{
        const date = `${{year}}-${{String(month + 1).padStart(2, '0')}}-${{String(day).padStart(2, '0')}}`;
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'day';
        button.textContent = String(day);
        if (availableDates.has(date)) {{
          button.classList.add('has-data');
          button.title = `${{date}} 有报告`;
          button.addEventListener('click', () => filterByDate(date));
        }} else {{
          button.disabled = true;
          button.title = `${{date}} 暂无报告`;
        }}
        if (date === selectedDate) {{
          button.classList.add('selected');
        }}
        calendarGrid.appendChild(button);
      }}
    }}

    document.getElementById('prevMonth').addEventListener('click', () => {{
      visibleMonth = new Date(visibleMonth.getFullYear(), visibleMonth.getMonth() - 1, 1);
      renderCalendar();
    }});
    document.getElementById('nextMonth').addEventListener('click', () => {{
      visibleMonth = new Date(visibleMonth.getFullYear(), visibleMonth.getMonth() + 1, 1);
      renderCalendar();
    }});
    document.getElementById('showAll').addEventListener('click', () => {{
      filterByDate('');
    }});
    document.getElementById('showLatest').addEventListener('click', () => {{
      if (sortedDates.length) {{
        visibleMonth = new Date(sortedDates[sortedDates.length - 1] + 'T00:00:00');
        filterByDate(sortedDates[sortedDates.length - 1]);
      }}
    }});
    input.addEventListener('change', () => {{
      if (input.value) visibleMonth = new Date(input.value + 'T00:00:00');
      filterByDate(input.value);
    }});
    const requestedDate = new URLSearchParams(window.location.search).get('date');
    if (requestedDate && /^\d{{4}}-\d{{2}}-\d{{2}}$/.test(requestedDate)) {{
      visibleMonth = new Date(requestedDate + 'T00:00:00');
      filterByDate(requestedDate);
    }}
    renderCalendar();
  </script>
</body>
</html>
"""


def main() -> None:
    ARCHIVE_FILE.write_text(_page_html(_report_dates()), encoding="utf-8")
    print(f"[site] archive generated: {ARCHIVE_FILE}")


if __name__ == "__main__":
    main()
