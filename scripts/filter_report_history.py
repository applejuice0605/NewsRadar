#!/usr/bin/env python3
"""Filter published HTML copies while preserving original reports and databases."""
import argparse
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

from generate_site_insights import peripheral_reason


class Node:
    def __init__(self, tag, attrs, start, parent=None):
        self.tag, self.attrs, self.start, self.parent = tag, dict(attrs), start, parent
        self.end = None
        self.children = []
        self.text = ''

    @property
    def classes(self):
        return set(self.attrs.get('class', '').split())

    def descendants(self):
        for child in self.children:
            yield child
            yield from child.descendants()


class Document(HTMLParser):
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.source = source
        self.lines = [0] + [i + 1 for i, char in enumerate(source) if char == '\n']
        self.root = Node('root', [], 0)
        self.stack = [self.root]
        self.body_end = None
        self.feed(source)

    def position(self):
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.position(), self.stack[-1])
        self.stack[-1].children.append(node)
        if tag in self.VOID:
            node.end = node.start + len(self.get_starttag_text())
        else:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            node = self.stack.pop()
            node.end = node.start + len(self.get_starttag_text())

    def handle_endtag(self, tag):
        if tag == 'body':
            self.body_end = self.position()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                for node in self.stack[index:]:
                    node.end = self.position() + len(tag) + 3
                del self.stack[index:]
                break

    def handle_data(self, data):
        for node in self.stack:
            node.text += data


ITEMS = {'news-item', 'new-item', 'rss-item', 'standalone-item'}
GROUPS = {'word-group', 'feed-group', 'new-source-group', 'standalone-group'}


def clean_report(source):
    doc = Document(source)
    nodes = list(doc.root.descendants())
    removed = []
    for node in nodes:
        if not node.classes & ITEMS or node.end is None:
            continue
        anchors = [child for child in node.descendants() if child.tag == 'a' and child.classes & {'news-link', 'rss-link'}]
        if any(peripheral_reason(anchor.text) for anchor in anchors):
            removed.append(node)
    if not removed:
        return source, 0
    edits = [(node.start, node.end, '') for node in removed]
    for group in nodes:
        if not group.classes & GROUPS or group.end is None:
            continue
        items = [item for item in group.descendants() if item.classes & ITEMS]
        kept = [item for item in items if item not in removed]
        if not items or len(kept) == len(items):
            continue
        if not kept:
            edits.append((group.start, group.end, ''))
            continue
        for badge in group.descendants():
            if badge.classes & {'word-count', 'feed-count'} and badge.end:
                edits.append((badge.start, badge.end, f'<span class="{" ".join(badge.classes)}">{len(kept)} 条</span>'))
            elif 'new-source-title' in badge.classes and badge.end:
                original = source[badge.start:badge.end]
                edits.append((badge.start, badge.end, re.sub(r'\d+\s*条', f'{len(kept)}条', original)))
        for index, item in enumerate(kept, 1):
            for label in item.descendants():
                if label.classes & {'news-number', 'new-item-number'} and label.end:
                    edits.append((label.start, label.end, f'<div class="{" ".join(label.classes)}">{index}</div>'))
    # Parent removals supersede child edits; all untouched source stays byte-identical.
    accepted = []
    for edit in sorted(edits, key=lambda edit: (edit[0], -edit[1])):
        if not accepted or edit[0] >= accepted[-1][1]:
            accepted.append(edit)
    for start, end, replacement in reversed(accepted):
        source = source[:start] + replacement + source[end:]
    updated = Document(source)
    updated_nodes = list(updated.root.descendants())
    counts = {}
    for group in updated_nodes:
        if 'word-group' in group.classes and 'data-tab-index' in group.attrs:
            counts[group.attrs['data-tab-index']] = sum('news-item' in item.classes for item in group.descendants())
    repairs = []
    for node in updated_nodes:
        if 'info-item' in node.classes:
            labels = [child for child in node.descendants() if 'info-label' in child.classes]
            values = [child for child in node.descendants() if 'info-value' in child.classes and child.end]
            if labels and values and labels[0].text.strip() == '热榜命中':
                value = values[0]
                original = source[value.start:value.end]
                repairs.append((value.start, value.end, re.sub(r'(>\s*)\d+', lambda m: m[1] + str(sum(counts.values())), original, count=1)))
        if 'new-section-title' in node.classes and node.end:
            parent = node.parent
            count = sum('new-item' in child.classes for child in parent.descendants())
            repairs.append((node.start, node.end, re.sub(r'共\s*\d+\s*条', f'共 {count} 条', source[node.start:node.end])))
    for button in updated_nodes:
        if 'tab-btn' not in button.classes or not button.end:
            continue
        index = button.attrs.get('data-tab-index')
        count = sum(counts.values()) if index == 'all' else counts.get(index, 0)
        if not count and index != 'all':
            repairs.append((button.start, button.end, ''))
            continue
        for badge in button.descendants():
            if 'tab-count' in badge.classes and badge.end:
                repairs.append((badge.start, badge.end, f'<span class="tab-count">{count}</span>'))
    for start, end, replacement in sorted(repairs, reverse=True):
        source = source[:start] + replacement + source[end:]
    notice = '<p class="history-quality-note">新闻列表已按当前周边过滤规则整理；原 AI 分析与播客保留生成时版本。</p>'
    parsed = Document(source)
    if parsed.body_end is not None:
        source = source[:parsed.body_end] + notice + source[parsed.body_end:]
    return source, len(removed)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--site-dir', type=Path, required=True)
    parser.add_argument('--backup', type=Path)
    args = parser.parse_args()
    paths = [args.site_dir / 'index.html'] + sorted((args.site_dir / 'reports').rglob('*.html'))
    paths = [path for path in paths if path.is_file()]
    if args.backup and not args.backup.exists():
        args.backup.parent.mkdir(parents=True, exist_ok=True)
        with ZipFile(args.backup, 'x', ZIP_DEFLATED) as archive:
            for path in paths:
                archive.write(path, str(path.relative_to(args.site_dir)))
    result = {'files_scanned': len(paths), 'files_changed': 0, 'items_removed': 0}
    for path in paths:
        before = path.read_text(encoding='utf-8')
        after, count = clean_report(before)
        if count:
            temporary = path.with_suffix('.quality.tmp')
            temporary.write_text(after, encoding='utf-8')
            temporary.replace(path)
            result['files_changed'] += 1
            result['items_removed'] += count
    args.site_dir.joinpath('history-quality.json').write_text(json.dumps(result, indent=2))
    print('[history-quality]', json.dumps(result))


if __name__ == '__main__':
    main()
