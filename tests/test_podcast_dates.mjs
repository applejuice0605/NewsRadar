import { test } from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';

const code = fs.readFileSync(new URL('../docs/assets/report-speech.js', import.meta.url), 'utf8');
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.attrs = {}; this.textContent = ''; }
  append(...items) { this.children.push(...items); }
  appendChild(item) { this.children.push(item); }
  insertBefore(item) { this.children.unshift(item); }
  setAttribute(key, value) { this.attrs[key] = value; }
  addEventListener() {}
}
async function render(metadata, pathname = '/') {
  const content = new Element('main');
  const info = { querySelector: key => ({ textContent: key === '.info-label' ? '生成时间' : '10-29 12:30' }) };
  const document = { readyState: 'complete', createElement: tag => new Element(tag),
    querySelector: () => content, querySelectorAll: () => [info] };
  const fetch = async () => ({ ok: true, json: async () => metadata });
  vm.runInNewContext(code, { document, window: { fetch, location: { pathname } }, fetch, Intl, Date, Number });
  await new Promise(resolve => setImmediate(resolve));
  const nodes = [];
  const walk = node => { nodes.push(node); node.children.forEach(walk); };
  walk(content);
  return nodes;
}

test('播报日期、新闻更新和音频生成分别展示；生成时间统一上海', async () => {
  const nodes = await render({ date: '2026-10-29', generated_at: '2026-10-29T01:02:03Z', audio_url: '/audio/2026-10-29/daily.mp3', voice: 'zh-CN-YunyangNeural' });
  assert(nodes.some(n => n.textContent === '新闻报告更新：10-29 12:30（北京时间）'));
  assert(nodes.some(n => n.textContent === '音频生成：2026-10-29 09:02:03（北京时间）'));
  assert(nodes.some(n => n.textContent.startsWith('播报日期：2026-10-29')));
  assert.equal(nodes.filter(n => n.tagName === 'audio').length, 1);
});

test('昨天音频不冒充当天；历史页面只播放对应日期', async () => {
  const metadata = { date: '2026-10-28', generated_at: 'invalid', audio_url: '/audio/2026-10-28/daily.mp3' };
  const today = await render(metadata);
  assert.equal(today.filter(n => n.tagName === 'audio').length, 0);
  assert(today.some(n => n.textContent.includes('最近一期为 2026-10-28')));
  const historical = await render(metadata, '/reports/2026-10-28/daily.html');
  assert.equal(historical.filter(n => n.tagName === 'audio').length, 1);
  assert(historical.some(n => n.textContent === '音频生成：未知（北京时间）'));
});
