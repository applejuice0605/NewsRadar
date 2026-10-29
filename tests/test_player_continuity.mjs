import { test } from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';

const code = fs.readFileSync(new URL('../docs/assets/report-speech.js', import.meta.url), 'utf8');
const css = fs.readFileSync(new URL('../docs/assets/report-speech.css', import.meta.url), 'utf8');
const KEY = 'newsradar.podcast.v1';
const metadata = { date: '2026-10-29', audio_url: '/audio/daily.mp3', source_hash: 'revision-a',
  voice: 'zh-CN-YunyangNeural', chapters: [{ title: '重点新闻', start_ratio: 0.5 },
    { title: '越界', start_seconds: 900 }, { title: '负数', start_seconds: -10 }] };
const episodeKey = (meta = metadata) => JSON.stringify([meta.date, meta.audio_url, meta.source_hash || 'legacy']);
class Events {
  listeners = new Map();
  addEventListener(name, fn, options = {}) {
    const entries = this.listeners.get(name) || [];
    entries.push({ fn, once: options.once }); this.listeners.set(name, entries);
  }
  dispatch(name, event = {}) {
    for (const entry of [...(this.listeners.get(name) || [])]) {
      if (entry.once) this.listeners.set(name, this.listeners.get(name).filter(e => e !== entry));
      entry.fn({ target: this, ...event });
    }
  }
}
class Element extends Events {
  constructor(tag) {
    super(); this.tagName = tag; this.children = []; this.attrs = {}; this.textContent = '';
    this.className = ''; this.style = { setProperty(k, v) { this[k] = v; } };
    this.currentTime = 0; this.duration = NaN; this.readyState = 0;
    this.paused = true; this.ended = false; this.playbackRate = 1; this.playCalls = 0;
    this.classList = {
      contains: name => this.className.split(' ').includes(name),
      toggle: (name, yes) => {
        const names = new Set(this.className.split(' ').filter(Boolean));
        if (yes) names.add(name); else names.delete(name);
        this.className = [...names].join(' ');
      }, remove: name => this.classList.toggle(name, false)
    };
  }
  append(...items) { this.children.push(...items); }
  appendChild(item) { this.append(item); }
  insertBefore(item) { this.children.unshift(item); }
  setAttribute(key, value) { this.attrs[key] = value; }
  getBoundingClientRect() { return { height: this.tagName === 'section' ? 320 : 156 }; }
  querySelectorAll(selector) { return walk(this).filter(n => n.classList.contains(selector.slice(1))); }
  play() { this.playCalls++; this.paused = false; this.ended = false; this.dispatch('play'); return Promise.resolve(); }
  pause() { this.paused = true; this.dispatch('pause'); }
  load() { this.loads = (this.loads || 0) + 1; }
  metadata(duration = 200) { this.duration = duration; this.readyState = 1; this.dispatch('loadedmetadata'); }
}
function walk(root) { return [root, ...root.children.flatMap(walk)]; }
function storage(initial = null) {
  return { value: initial, writes: 0, getItem(key) { assert.equal(key, KEY); return this.value; },
    setItem(key, value) { assert.equal(key, KEY); this.value = value; this.writes++; } };
}
function seed(position, extra = {}, meta = metadata) {
  return JSON.stringify({ rate: 1, entries: [{ key: episodeKey(meta), position, updatedAt: Date.now(), ...extra }] });
}
async function render({ store = storage(), meta = metadata, path = '/', denied = false } = {}) {
  const content = new Element('main');
  const document = new Events();
  document.readyState = 'complete'; document.documentElement = new Element('html');
  document.createElement = tag => new Element(tag);
  document.querySelector = () => content;
  document.querySelectorAll = () => [{ querySelector: selector => ({ textContent: selector === '.info-label' ? '生成时间' : '10-29 12:30' }) }];
  const window = new Events(); window.location = { pathname: path };
  Object.defineProperty(window, 'localStorage', { get() { if (denied) throw new Error('SecurityError'); return store; } });
  const fetch = async () => ({ ok: true, json: async () => meta }); window.fetch = fetch;
  const observers = [];
  class IntersectionObserver {
    constructor(callback) { this.callback = callback; observers.push(this); }
    observe(target) { this.target = target; }
    visible(value) { this.callback([{ isIntersecting: value }]); }
  }
  window.IntersectionObserver = IntersectionObserver;
  let now = Date.now();
  class Clock extends Date { static now() { return now; } }
  vm.runInNewContext(code, { document, window, fetch, IntersectionObserver, Intl, Date: Clock });
  await new Promise(resolve => setImmediate(resolve));
  const nodes = walk(content);
  const find = cls => nodes.find(node => node.classList.contains(cls));
  return { audio: nodes.find(node => node.tagName === 'audio'), find, nodes, document, window, observers, store,
    advance(ms) { now += ms; }, entry() { return JSON.parse(store.value).entries.find(e => e.key === episodeKey(meta)); } };
}

test('restore after metadata without autoplay; persistent explicit resume plays on click across full loads', async () => {
  const store = storage(seed(73));
  const page = await render({ store });
  assert.equal(page.audio.currentTime, 0);
  assert.equal(page.find('podcast-resume').hidden, false);
  assert.match(page.find('podcast-resume').textContent, /1:13/);
  page.audio.metadata();
  assert.equal(page.audio.currentTime, 73); assert.equal(page.audio.playCalls, 0);
  page.find('podcast-resume').dispatch('click');
  assert.equal(page.audio.playCalls, 1);
  page.audio.currentTime = 84; page.window.dispatch('pagehide');
  const next = await render({ store, path: '/reports/2026-10-29/daily.html' });
  next.audio.metadata();
  assert.equal(next.audio.currentTime, 84); assert.equal(next.audio.playCalls, 0);
  assert.match(next.find('podcast-resume-note').textContent, /切换页面会停止播放/);
});

test('timeupdate is throttled to five seconds; pause, navigation and pagehide flush', async () => {
  const p = await render(); p.audio.metadata();
  p.audio.currentTime = 10; p.audio.dispatch('timeupdate');
  const writes = p.store.writes;
  p.advance(4999); p.audio.currentTime = 11; p.audio.dispatch('timeupdate');
  assert.equal(p.store.writes, writes);
  p.advance(1); p.audio.currentTime = 12; p.audio.dispatch('timeupdate');
  assert.equal(p.entry().position, 12); assert.equal(p.store.writes, writes + 1);
  p.audio.currentTime = 13; p.audio.pause(); assert.equal(p.entry().position, 13);
  p.audio.currentTime = 14;
  p.document.dispatch('click', { target: { closest: () => ({ href: '/reports/' }) } });
  assert.equal(p.entry().position, 14);
  p.audio.currentTime = 15; p.window.dispatch('pagehide'); assert.equal(p.entry().position, 15);
});

test('explicit labelled rate select offers all rates and persists to a new page', async () => {
  const p = await render(); const select = p.find('podcast-rate');
  assert.equal(select.tagName, 'select'); assert.equal(select.attrs['aria-label'], '播放速度');
  assert.deepEqual(select.children.map(o => o.value), ['0.8', '1', '1.25', '1.5', '2']);
  for (const value of ['0.8', '1', '1.25', '1.5', '2']) {
    select.value = value; select.dispatch('change'); assert.equal(p.audio.playbackRate, Number(value));
  }
  const next = await render({ store: p.store });
  assert.equal(next.audio.playbackRate, 2); assert.equal(next.find('podcast-rate').value, '2');
  assert.equal(next.audio.playCalls, 0);
});

test('corrupt, invalid, denied and quota-limited storage do not prevent controls or chapters', async () => {
  for (const value of ['{bad', 'null', '[]', '{"rate":99,"entries":[null,{"key":"bad","position":-1}]}']) {
    const p = await render({ store: storage(value) });
    p.audio.metadata(); assert.equal(p.audio.playbackRate, 1);
    p.find('podcast-chapter').dispatch('click'); assert.equal(p.audio.currentTime, 100);
  }
  const quota = storage(); quota.setItem = () => { throw new Error('QuotaExceededError'); };
  for (const options of [{ denied: true }, { store: quota }]) {
    const p = await render(options); p.audio.metadata();
    p.find('podcast-rate').value = '1.5'; p.find('podcast-rate').dispatch('change');
    assert.equal(p.audio.playbackRate, 1.5);
    p.audio.pause(); p.window.dispatch('pagehide');
    assert.match(p.find('podcast-resume-note').textContent, /无法保存进度/);
    p.find('podcast-chapter').dispatch('click'); assert.equal(p.audio.playCalls, 1);
  }
});

test('date, url and source revision isolate progress; missing hash uses url/date legacy identity', async () => {
  const store = storage(seed(60));
  for (const change of [{ source_hash: 'revision-b' }, { audio_url: '/new.mp3' }, { date: '2026-10-28' }]) {
    const meta = { ...metadata, ...change };
    const p = await render({ store, meta, path: `/reports/${meta.date}/daily.html` });
    p.audio.metadata(); assert.equal(p.audio.currentTime, 0); assert.equal(p.find('podcast-resume').hidden, true);
  }
  const legacy = { ...metadata }; delete legacy.source_hash;
  const p = await render({ store: storage(seed(42, {}, legacy)), meta: legacy });
  p.audio.metadata(); assert.equal(p.audio.currentTime, 42);
  const revised = await render({ store: p.store }); revised.audio.metadata(); assert.equal(revised.audio.currentTime, 0);
});

test('resume and chapter seeks clamp duration boundaries, including very short audio', async () => {
  for (const [duration, expected] of [[50, 49.75], [0.1, 0]]) {
    const p = await render({ store: storage(seed(1000)) }); p.audio.metadata(duration);
    assert.equal(p.audio.currentTime, expected);
    const chapters = p.nodes.filter(n => n.classList.contains('podcast-chapter'));
    chapters[1].dispatch('click'); assert.equal(p.audio.currentTime, expected);
    chapters[2].dispatch('click'); assert.equal(p.audio.currentTime, 0);
    assert.equal(chapters[2].attrs['aria-pressed'], 'true');
  }
  const p = await render({ store: storage(seed(37)) }); p.audio.metadata(Infinity);
  assert.equal(p.audio.currentTime, 0); assert.equal(p.audio.playCalls, 0);
  p.window.dispatch('pagehide'); assert.equal(p.entry().position, 37);
  p.audio.metadata(100); assert.equal(p.audio.currentTime, 37);
});

test('resume before metadata waits; navigation before metadata preserves saved position', async () => {
  const p = await render({ store: storage(seed(37)) });
  p.window.dispatch('pagehide'); assert.equal(p.entry().position, 37);
  p.find('podcast-resume').dispatch('click'); assert.equal(p.audio.playCalls, 0);
  assert.equal(p.audio.loads, 1); p.audio.metadata();
  assert.equal(p.audio.currentTime, 37); assert.equal(p.audio.playCalls, 1);
});

test('ended marks complete and clears position even after pause/pagehide; replay can save again', async () => {
  const p = await render({ store: storage(seed(80)) }); p.audio.metadata();
  p.audio.currentTime = 200; p.audio.ended = true; p.audio.paused = true; p.audio.dispatch('ended');
  p.audio.pause(); p.window.dispatch('pagehide');
  assert.equal(p.entry().position, 0); assert.equal(p.entry().completed, true);
  const next = await render({ store: p.store }); next.audio.metadata();
  assert.equal(next.audio.currentTime, 0); assert.equal(next.find('podcast-resume').hidden, true);
  next.audio.play(); next.audio.currentTime = 6; next.audio.pause();
  assert.equal(next.entry().completed, false); assert.equal(next.entry().position, 6);
});

test('storage retention prunes over ninety days and caps to latest 100 entries', async () => {
  const now = Date.now();
  const entries = Array.from({ length: 120 }, (_, i) => ({ key: `episode-${i}`, position: 1, updatedAt: now - i * 1000 }));
  entries.push({ key: 'expired', position: 1, updatedAt: now - 91 * 86400000 });
  const p = await render({ store: storage(JSON.stringify({ rate: 1, entries })) });
  p.audio.metadata(); p.audio.currentTime = 5; p.audio.pause();
  const saved = JSON.parse(p.store.value).entries;
  assert.equal(saved.length, 100); assert.equal(saved[0].key, episodeKey());
  assert(!saved.some(e => e.key === 'expired' || e.key === 'episode-119'));
});

test('mini player reserves content space, stops on pause/ended, works after replay and resize', async () => {
  const p = await render(); const slot = p.find('podcast-slot'); const observer = p.observers[0];
  observer.visible(false); assert.equal(slot.classList.contains('is-mini'), false);
  p.audio.play(); assert.equal(slot.classList.contains('is-mini'), true);
  assert.equal(p.document.documentElement.style['--podcast-mini-space'], '188px');
  assert.equal(observer.target.style.minHeight, '320px');
  p.window.dispatch('resize'); assert.equal(slot.classList.contains('is-mini'), true);
  p.audio.pause(); assert.equal(slot.classList.contains('is-mini'), false);
  assert.equal(p.document.documentElement.style['--podcast-mini-space'], '0px');
  p.audio.play(); assert.equal(slot.classList.contains('is-mini'), true);
  p.audio.ended = true; p.audio.dispatch('ended'); assert.equal(slot.classList.contains('is-mini'), false);
  p.audio.play(); observer.visible(true); assert.equal(slot.classList.contains('is-mini'), false);
});

test('mobile controls remain semantic, touch sized, wrapping, safe-area aware and motion reduced', async () => {
  const p = await render({ store: storage(seed(40)) }); p.audio.metadata();
  assert.equal(p.audio.controls, true);
  assert.equal(p.find('podcast-resume').tagName, 'button');
  assert.match(p.find('podcast-resume').attrs['aria-label'], /0:40/);
  assert(p.nodes.filter(n => n.classList.contains('podcast-chapter')).every(n => n.tagName === 'button' && n.attrs['aria-label']));
  // Fake DOM cannot lay out pixels: assert the actual stylesheet's guardrails separately.
  assert.match(css, /min-height: 44px/); assert.match(css, /min-width: 44px/);
  assert.match(css, /flex-wrap: wrap/); assert.match(css, /overflow-wrap: anywhere/);
  assert.match(css, /env\(safe-area-inset-bottom/); assert.match(css, /body::after/);
  assert.match(css, /prefers-reduced-motion: reduce/);
});

test('two tabs: inactive B at 10 cannot overwrite A at 80 on pause/navigation/pagehide, even before storage delivery', async () => {
  const store = storage(seed(10));
  const a = await render({ store }); const b = await render({ store });
  a.audio.metadata(); b.audio.metadata();
  assert.equal(b.audio.currentTime, 10);
  // Metadata restoration queues media events in browsers; those are not local seeks.
  b.audio.dispatch('seeked'); b.audio.dispatch('timeupdate');
  await a.audio.play(); a.audio.currentTime = 80; a.audio.pause();
  const freshEntry = a.entry(); const writes = store.writes;
  // A storage event may be delayed until after B exits. The fresh read must suffice.
  b.audio.pause();
  b.document.dispatch('click', { target: { closest: () => ({ href: '/reports/' }) } });
  b.window.dispatch('pagehide');
  assert.deepEqual(b.entry(), freshEntry); assert.equal(store.writes, writes);
  assert.match(b.find('podcast-resume').textContent, /1:20/);
  assert.equal(b.audio.currentTime, 10); assert.equal(b.audio.playCalls, 0);
  b.find('podcast-resume').dispatch('click');
  assert.equal(b.audio.currentTime, 80); assert.equal(b.audio.playCalls, 1);
  // Actual new local listening is allowed to advance the shared position.
  b.audio.currentTime = 85; b.audio.pause(); assert.equal(b.entry().position, 85);
});

test('two tabs: storage events update inactive pending resume before/after metadata without seeking or autoplay', async () => {
  const store = storage(seed(10));
  const a = await render({ store }); const b = await render({ store });
  a.audio.metadata(); await a.audio.play(); a.audio.currentTime = 80; a.audio.pause();
  b.window.dispatch('storage', { key: 'unrelated', newValue: store.value });
  assert.match(b.find('podcast-resume').textContent, /0:10/);
  b.window.dispatch('storage', { key: KEY, newValue: store.value });
  assert.match(b.find('podcast-resume').textContent, /1:20/);
  assert.equal(b.audio.currentTime, 0); assert.equal(b.audio.playCalls, 0);
  b.audio.metadata(); assert.equal(b.audio.currentTime, 80);
  a.audio.currentTime = 95; a.audio.dispatch('seeked');
  b.window.dispatch('storage', { key: KEY, newValue: store.value });
  assert.match(b.find('podcast-resume').textContent, /1:35/);
  assert.equal(b.audio.currentTime, 80); assert.equal(b.audio.playCalls, 0);
  b.audio.dispatch('timeupdate'); b.window.dispatch('pagehide');
  assert.equal(b.entry().position, 95);
  // An externally completed episode clears the pending resume without playing B.
  a.audio.ended = true; a.audio.currentTime = 200; a.audio.dispatch('ended');
  b.window.dispatch('storage', { key: KEY, newValue: store.value });
  assert.equal(b.find('podcast-resume').hidden, true);
  b.window.dispatch('pagehide'); assert.equal(b.entry().completed, true);
});

test('two tabs: rate-only edits merge preferences without replacing position or its timestamp', async () => {
  const store = storage(seed(10));
  const a = await render({ store }); const b = await render({ store });
  a.audio.metadata(); b.audio.metadata();
  a.audio.currentTime = 80; a.audio.dispatch('seeked');
  const freshEntry = a.entry();
  // Deliberately do not deliver A's storage event before B changes rate.
  b.find('podcast-rate').value = '1.5'; b.find('podcast-rate').dispatch('change');
  assert.deepEqual(b.entry(), freshEntry); assert.equal(JSON.parse(store.value).rate, 1.5);
  a.window.dispatch('storage', { key: KEY, newValue: store.value });
  assert.equal(a.audio.playbackRate, 1.5); assert.equal(a.find('podcast-rate').value, '1.5');
  const writes = store.writes;
  a.audio.dispatch('ratechange'); a.window.dispatch('pagehide'); b.window.dispatch('pagehide');
  assert.equal(store.writes, writes); assert.deepEqual(a.entry(), freshEntry);
  // Saving progress in a tab that missed a newer preference must keep that preference.
  b.find('podcast-rate').value = '2'; b.find('podcast-rate').dispatch('change');
  a.audio.currentTime = 90; a.audio.dispatch('seeked');
  assert.equal(a.entry().position, 90); assert.equal(JSON.parse(store.value).rate, 2);
});

test('two tabs: active or locally dirty progress survives remote updates; deliberate backwards seek is saved', async () => {
  const store = storage(seed(10));
  const a = await render({ store }); const b = await render({ store });
  a.audio.metadata(); b.audio.metadata();
  await b.audio.play(); b.audio.currentTime = 11; b.audio.dispatch('timeupdate');
  b.audio.currentTime = 12; b.audio.dispatch('timeupdate'); // throttled, still locally dirty
  a.audio.currentTime = 80; a.audio.dispatch('seeked');
  b.window.dispatch('storage', { key: KEY, newValue: store.value });
  assert.equal(b.audio.currentTime, 12);
  b.audio.pause(); assert.equal(b.entry().position, 12);
  // A missed the update, but an intentional seek still owns its local new position.
  a.audio.currentTime = 5; a.audio.dispatch('seeked'); assert.equal(a.entry().position, 5);
  const writes = store.writes;
  b.window.dispatch('storage', { key: KEY, newValue: store.value });
  b.window.dispatch('pagehide'); assert.equal(b.entry().position, 5); assert.equal(store.writes, writes);
});

test('source reason and measured duration spans are preserved alongside continuity controls', async () => {
  const p = await render({ meta: { ...metadata, source_reason: '来源已核对', duration_seconds: 125.4 } });
  assert(p.nodes.some(n => n.textContent === '来源已核对'));
  assert(p.nodes.some(n => n.textContent === '实测时长：125 秒'));
  assert(p.find('podcast-rate'));
});
