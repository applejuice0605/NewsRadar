#!/usr/bin/env python3
"""Refresh preview and send a verified daily Feishu health report."""
import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get('NEWSRADAR_PRODUCTION_DIR', '/Users/bruce/Coding/NewsRadar-production'))
HEALTH_CONFIG = REPO / '.health.env.local'


def display_china_time(value):
    if not value:
        return '未知'
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo('Asia/Shanghai'))
        return parsed.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')
    except ValueError:
        return '未知'


def build_health_card(state, runtime, now):
    notification = runtime.get('notification', {})
    collection = runtime.get('collection', {})
    podcast = state.get('podcast', {})
    healthy = (state.get('deploy_success') and state.get('website_fresh')
               and podcast.get('status') == 'success'
               and notification.get('status') not in ('failed', 'disabled', 'unknown')
               and collection.get('success') is True)
    labels = {'success': '成功', 'failed': '失败', 'skipped': '本轮无需推送', 'disabled': '已禁用', 'unknown': '未知'}

    def group(title, content):
        color = {'新闻看板与采集': 'blue', 'AI 播客': 'turquoise', '飞书新闻推送': 'orange'}[title]
        return {'tag': 'column_set', 'flex_mode': 'none', 'columns': [
            {'tag': 'column', 'width': 'weighted', 'weight': 1,
             'direction': 'vertical', 'vertical_spacing': '8px',
             'background_style': 'grey-50', 'padding': '12px',
             'elements': [
                 {'tag': 'markdown', 'content': "<text_tag color='" + color + "'>" + title + '</text_tag>'},
                 {'tag': 'markdown', 'content': content},
             ]}]}

    card = {
        'schema': '2.0',
        'config': {'width_mode': 'default', 'summary': {'content': 'NewsRadar 健康检查：' + ('正常' if healthy else '需关注')}},
        'header': {'title': {'tag': 'plain_text', 'content': 'NewsRadar 每日健康报告'},
                   'subtitle': {'tag': 'plain_text', 'content': now.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M') + ' · 以下时间均为北京时间'},
                   'template': 'green' if healthy else 'red'},
        'body': {'direction': 'vertical', 'vertical_spacing': '12px', 'elements': [
            {'tag': 'markdown', 'text_size': 'heading-2', 'content': '**' + ('系统运行正常' if healthy else '系统存在需要关注的项目') + '**'},
            group('新闻看板与采集',
                  '内测部署：' + ('成功' if state.get('deploy_success') else '失败') +
                  '\n线上数据：' + ('正常' if state.get('website_fresh') else '异常/过期') +
                  '\n距上次采集完成：' + (str(int(float(state['website_age_minutes']) + 0.5)) + ' 分钟' if state.get('website_age_minutes') is not None else '未知') +
                  '\n上次采集完成：' + display_china_time(collection.get('last_completed_at')) +
                  '\n间隔后补跑：' + ('是' if collection.get('catch_up') else '否')),
            group('AI 播客', '生成状态：' + labels.get(podcast.get('status'), '未知') +
                  '\n最新一期：' + str(podcast.get('date', '未知')) +
                  '\n线上音频：' + ('可访问' if podcast.get('online_verified') else '验证失败')),
            group('飞书新闻推送', '状态：' + labels.get(notification.get('status'), '未知') +
                  '\n上次推送成功：' + display_china_time(notification.get('last_success_at'))),
            {'tag': 'markdown', 'content': '[查看完整运行状态](https://preview.newsradar.ccwu.cc/status)'},
        ]},
    }
    elements = card['body']['elements']
    sections = elements[1:-1]
    card['body']['elements'] = [elements[0], {
        'tag': 'column_set', 'flex_mode': 'none',
        'horizontal_spacing': '12px',
        'columns': [section['columns'][0] for section in sections],
    }, elements[-1]]
    return card


def send_health_card(card, idempotency_key):
    config = env_file(HEALTH_CONFIG)
    webhook = config.get('NEWSRADAR_HEALTH_WEBHOOK_URL', '')
    secret = config.get('NEWSRADAR_HEALTH_WEBHOOK_SECRET', '')
    if not webhook.startswith(('https://open.larksuite.com/open-apis/bot/v2/hook/', 'https://open.feishu.cn/open-apis/bot/v2/hook/')) or not secret:
        raise RuntimeError('Dedicated health webhook or signature secret is missing')
    timestamp = str(int(time.time()))
    signature = base64.b64encode(hmac.new(
        (timestamp + '\n' + secret).encode(), b'', hashlib.sha256,
    ).digest()).decode()
    payload = {'timestamp': timestamp, 'sign': signature, 'msg_type': 'interactive', 'card': card}
    request = urllib.request.Request(webhook, data=json.dumps(payload, ensure_ascii=False).encode(), headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            answer = json.load(response)
    except (urllib.error.URLError, ValueError):
        raise RuntimeError('Health webhook request failed; URL and secret redacted') from None
    if answer.get('code', answer.get('StatusCode', -1)) != 0:
        raise RuntimeError('Health card rejected, code=' + str(answer.get('code', answer.get('StatusCode'))))
    return answer.get('data') or {}


def fetch_private(url, token, method='GET', byte_range=None):
    headers = {'Authorization': 'Basic ' + token, 'User-Agent': 'Mozilla/5.0 NewsRadarHealth/1.0', 'Cache-Control': 'no-cache'}
    if byte_range:
        headers['Range'] = byte_range
    request = urllib.request.Request(url, method=method, headers=headers)
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
        with fetch_private('https://preview.newsradar.ccwu.cc' + audio_url, token, byte_range='bytes=0-31') as response:
            audio_ok = response.status in (200, 206) and response.headers.get('Content-Type', '').startswith('audio/') and len(response.read(32)) == 32
        state['podcast']['online_verified'] = audio_ok and remote_podcast.get('date') == podcast.get('date')
    except Exception:
        state['podcast']['online_verified'] = False
    if not state['podcast']['online_verified']:
        state['podcast']['status'] = 'failed'
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    if args.notify or (now.hour >= 22 and state.get('last_daily_sent') != now.date().isoformat()):
        runtime = read_json(DATA / 'TrendRadar/output/meta/runtime_health.json')
        card = build_health_card(state, runtime, now)
        key = 'nr-health-' + (now.strftime('%Y%m%d-%H%M%S') if args.notify else now.strftime('%Y%m%d'))
        receipt = send_health_card(card, key)
        state['health_channel'] = 'dedicated_signed_webhook'
        state['health_message_id'] = receipt.get('message_id')
        state['last_daily_sent'] = now.date().isoformat() if now.hour >= 22 else None
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        print('[health] Feishu daily report delivered')
    if not state['deploy_success'] or not state['website_fresh']:
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
