"""Exact-title delivery deduplication; semantic matches never block delivery."""
import copy
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def identity(item):
    title = re.sub(r'\s+', '', str(item.get('title', ''))).casefold()
    return hashlib.sha256(title.encode()).hexdigest() if title else None


class DeliveryLedger:
    def __init__(self, path, account):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.account = hashlib.sha256(account.encode()).hexdigest()
        self.conn = sqlite3.connect(self.path, timeout=10)
        self.conn.execute('CREATE TABLE IF NOT EXISTS delivered (account TEXT, fingerprint TEXT, title TEXT, delivered_at TEXT, PRIMARY KEY(account, fingerprint))')

    def close(self):
        self.conn.close()

    def delivered(self, key):
        return self.conn.execute('SELECT 1 FROM delivered WHERE account=? AND fingerprint=?', (self.account, key)).fetchone() is not None

    def record_success(self, items):
        stamp = datetime.now(timezone.utc).isoformat()
        with self.conn:
            self.conn.executemany('INSERT OR IGNORE INTO delivered VALUES (?,?,?,?)',
                [(self.account, identity(item), item.get('title', ''), stamp) for item in items if identity(item)])

    def filter_payload(self, report, rss, rss_new, block_delivered=True):
        rd, ri, rn = copy.deepcopy((report, rss, rss_new))
        counters = {'candidate_count': 0, 'within_batch_duplicate_count': 0,
                    'cross_source_duplicate_count': 0, 'delivered_blocked_count': 0}
        seen = {}
        selected = []
        accepted_keys = set()

        def filter_groups(groups, auxiliary=False):
            output = []
            for group in groups or []:
                titles = []
                for item in group.get('titles', []):
                    key = identity(item)
                    if not key:
                        titles.append(item)
                        continue
                    if auxiliary:
                        if key in accepted_keys:
                            titles.append(item)
                        continue
                    counters['candidate_count'] += 1
                    source = item.get('source_id') or item.get('source_name') or group.get('source_id') or group.get('source_name') or group.get('feed_id')
                    if key in seen:
                        counters['within_batch_duplicate_count'] += 1
                        if seen[key] != source:
                            counters['cross_source_duplicate_count'] += 1
                        continue
                    seen[key] = source
                    if block_delivered and self.delivered(key):
                        counters['delivered_blocked_count'] += 1
                        continue
                    accepted_keys.add(key)
                    selected.append(item)
                    titles.append(item)
                if titles:
                    group['titles'] = titles
                    group['count'] = len(titles)
                    output.append(group)
            return output

        rd['stats'] = filter_groups(rd.get('stats'))
        ri = filter_groups(ri)
        rd['new_titles'] = filter_groups(rd.get('new_titles'), auxiliary=True)
        rn = filter_groups(rn, auxiliary=True)
        rd['total_new_count'] = min(int(rd.get('total_new_count', 0)), len(selected))
        total = counters['candidate_count']
        counters['duplicate_rate_percent'] = round(100 * counters['within_batch_duplicate_count'] / total, 1) if total else None
        counters['mode'] = 'exact_title_delivery_gate'
        counters['measured_at'] = datetime.now(timezone.utc).isoformat()
        return rd, ri, rn, selected, counters


def save_delivery_metrics(directory, account_hash, counters):
    path = Path(directory) / 'delivery_dedup.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        state = {'accounts': {}}
    state.setdefault('accounts', {})[account_hash] = counters
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    temporary.replace(path)
