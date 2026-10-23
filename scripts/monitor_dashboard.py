#!/usr/bin/env python3
"""Refresh preview and send a verified daily Feishu health report."""
import argparse
import base64
import json
import os
import re
import subprocess
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get('NEWSRADAR_PRODUCTION_DIR', '/Users/bruce/Coding/NewsRadar-production'))


def fetch_private(url, token, method='GET'):
    request = urllib.request.Request(url, method=method, headers={'Authorization': 'Basic ' + token, 'User-Agent': 'Mozilla/5.0 NewsRadarHealth/1.0', 'Cache-Control': 'no-cache'})
    return urllib.request.urlopen(request, timeout=45)


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def env_file(path):
    result = {}
    for line in path.read_text().splitlines():
        match = re.match(r'^(?:export\s+)?([A-Z_]+)=(.*)$', line.strip())
        if match:
            result[match[1]] = match[2].strip().strip('\"\'')
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--notify', action='store_true')
    args = parser.parse_args()
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    state_path = REPO / '.deploy/dashboard-health.json'
    previous = read_json(state_path)
    state = {'checked_at': now.isoformat(), 'last_daily_sent': previous.get('last_daily_sent')}
    result = subprocess.run([str(REPO / 'scripts/deploy_preview.sh')], cwd=REPO)
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    state['deploy_success'] = result.returncode == 0
    auth = env_file(REPO / '.preview-auth.local')
    token = base64.b64encode((auth.get('PREVIEW_USERNAME', 'newsradar') + ':' + auth['PREVIEW_PASSWORD']).encode()).decode()
    try:
        with fetch_private('https://preview.newsradar.ccwu.cc/health.json', token) as response:
            health = json.load(response)
        completed = health.get('collection', {}).get('last_completed_at')
        age = (now - datetime.fromisoformat(completed)).total_seconds() / 60 if completed else None
        state['website_age_minutes'] = round(age, 1) if age is not None else None
        state['website_fresh'] = age is not None and age < 120
    except Exception as error:
        state['website_fresh'] = False
        state['error_type'] = type(error).__name__
        health = {}
    podcast = read_json(DATA / 'TrendRadar/output/audio/latest.json')
    expected_date = now.date().isoformat() if now.hour >= 22 else (now.date().fromordinal(now.date().toordinal() - 1)).isoformat()
    audio = DATA / ('TrendRadar/output' + str(podcast.get('audio_url', '')))
    state['podcast'] = {'date': podcast.get('date'), 'generated_at': podcast.get('generated_at'), 'voice': podcast.get('voice'), 'status': 'success' if podcast.get('date', '') >= expected_date and audio.is_file() and audio.stat().st_size > 1000 else 'failed'}
    try:
        with fetch_private('https://preview.newsradar.ccwu.cc/audio/latest.json', token) as response:
            remote_podcast = json.load(response)
        audio_url = str(remote_podcast.get('audio_url', ''))
        if not audio_url.startswith('/audio/') or '..' in audio_url:
            raise ValueError('invalid audio path')
        with fetch_private('https://preview.newsradar.ccwu.cc' + audio_url, token, 'HEAD') as response:
            audio_ok = response.status == 200 and int(response.headers.get('Content-Length', '0')) > 1000
        state['podcast']['online_verified'] = audio_ok and remote_podcast.get('date') == podcast.get('date')
    except Exception:
        state['podcast']['online_verified'] = False
    if not state['podcast']['online_verified']:
        state['podcast']['status'] = 'failed'
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    if args.notify or (now.hour >= 22 and state.get('last_daily_sent') != now.date().isoformat()):
        runtime = read_json(DATA / 'TrendRadar/output/meta/runtime_health.json')
        notification = runtime.get('notification', {})
        text = '\n'.join([
            'NewsRadar 每日健康检查 · ' + now.strftime('%Y-%m-%d %H:%M'),
            '内测部署：' + ('成功' if state['deploy_success'] else '失败'),
            '网站新闻：' + ('正常' if state['website_fresh'] else '异常/过期') + '，距最近采集 ' + str(state.get('website_age_minutes')) + ' 分钟',
            '最近采集：' + str(runtime.get('collection', {}).get('last_completed_at')),
            '睡眠/调度间隔后补跑：' + str(runtime.get('collection', {}).get('catch_up')),
            'AI 播客：' + state['podcast']['status'] + '，最新日期 ' + str(podcast.get('date')),
            '飞书新闻推送：' + str(notification.get('status')) + '，最近成功 ' + str(notification.get('last_success_at')),
            'https://preview.newsradar.ccwu.cc/status',
        ])
        secrets = env_file(DATA / 'TrendRadar/.env.local')
        webhook = secrets.get('FEISHU_WEBHOOK_URL', '')
        if not webhook:
            raise RuntimeError('FEISHU_WEBHOOK_URL is missing')
        request = urllib.request.Request(webhook, data=json.dumps({'msg_type': 'text', 'content': {'text': text}}).encode(), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=30) as response:
            answer = json.load(response)
        if answer.get('code', answer.get('StatusCode', -1)) != 0:
            raise RuntimeError('Feishu did not confirm delivery')
        state['last_daily_sent'] = now.date().isoformat() if now.hour >= 22 else None
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        print('[health] Feishu daily report delivered')
    if not state['deploy_success'] or not state['website_fresh']:
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
