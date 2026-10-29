import assert from "node:assert/strict";
import { test, after } from "node:test";
import { webcrypto, createHmac } from "node:crypto";
import { inspectHealth, scheduled, STATE_KEY, shanghaiTime } from "../workers/dashboard-monitor.mjs";
import worker from "../workers/preview-auth.mjs";

// All I/O is mocked. Any unexpected notification attempt fails without networking.
const originalFetch = globalThis.fetch;
const originalNow = Date.now;
const originalCrypto = globalThis.crypto;
const originalLog = console.log;
const originalError = console.error;
Object.defineProperty(globalThis, "crypto", { configurable: true, value: webcrypto });
let now = Date.parse("2026-10-29T14:00:00Z"); // 22:00 Shanghai
Date.now = () => now;
const logs = [];
console.log = message => logs.push(message);
console.error = message => logs.push(message);
let requests = [];
let delivery = "success";
globalThis.fetch = async (url, options) => {
  assert.equal(url, "https://bot.invalid/secret-token");
  assert.equal(options.redirect, "manual");
  requests.push(JSON.parse(options.body));
  if (delivery === "throw") throw new Error("https://bot.invalid/secret-token test-secret");
  if (delivery === "http") return new Response("private", { status: 500 });
  if (delivery === "json") return new Response("private");
  return Response.json({ code: delivery === "reject" ? 19021 : 0 });
};
after(() => {
  globalThis.fetch = originalFetch;
  Date.now = originalNow;
  Object.defineProperty(globalThis, "crypto", { configurable: true, value: originalCrypto });
  console.log = originalLog;
  console.error = originalError;
});

function fixture(at = now) {
  const day = shanghaiTime(at).slice(0, 10);
  const data = {
    health: { collection: { success: true, last_completed_at: new Date(at - 60_000).toISOString() } },
    audio: { date: day, generated_at: new Date(at - 60_000).toISOString(), audio_url: `/audio/${day}/daily.mp3` },
    bytes: new Uint8Array([1, 2, 3, 4]), type: "audio/mpeg", status: 206, range: "bytes 0-3/4",
  };
  const storage = new Map();
  const seen = [];
  const env = {
    HEALTH_WEBHOOK_URL: "https://bot.invalid/secret-token", HEALTH_WEBHOOK_SECRET: "test-secret",
    HEALTH_MONITOR_STATE: {
      get: async key => storage.get(key) ?? null,
      put: async (key, value) => storage.set(key, value),
    },
    ASSETS: { fetch: async request => {
      assert.equal(request.redirect, "manual"); // Workers does not support redirect:error
      seen.push(request);
      const path = new URL(request.url).pathname;
      if (path === "/health.json") {
        if (data.healthError) throw new Error("private asset URL");
        return data.healthResponse?.() ?? Response.json(data.health);
      }
      if (path === "/audio/latest.json") return data.audioResponse?.() ?? Response.json(data.audio);
      assert.equal(request.headers.get("Range"), "bytes=0-31");
      if (data.resourceError) throw new Error("private resource URL");
      return new Response(data.bytes, { status: data.status, headers: { "Content-Type": data.type, "Content-Range": data.range } });
    } },
  };
  return { env, data, storage, seen };
}

test("scheduled export, direct synthetic assets, Shanghai details, healthy first check silent", async () => {
  requests = [];
  assert.equal(worker.scheduled, scheduled);
  const f = fixture();
  await scheduled({}, f.env);
  const state = JSON.parse(f.storage.get(STATE_KEY));
  assert.equal(state.status, "healthy");
  assert.equal(state.alert_open, false);
  assert.equal(requests.length, 0);
  assert.equal(f.seen.length, 3);
  assert.ok(f.seen.every(request => request.url.startsWith("https://preview.newsradar.ccwu.cc/") && !request.headers.has("Authorization")));
  assert.match(state.details.join("\n"), /2026-10-29 21:59:00；距今 1 分钟/);
});

test("collection boundaries, missing/invalid/future timestamps and success are never falsely healthy", async () => {
  for (const [minutes, expected] of [[120, "healthy"], [120.01, "unhealthy"], [-5, "healthy"], [-5.01, "unknown"]]) {
    const f = fixture();
    f.data.health.collection.last_completed_at = new Date(now - minutes * 60_000).toISOString();
    assert.equal((await inspectHealth(f.env, now)).status, expected, String(minutes));
  }
  for (const value of [undefined, null, 0, "bad", "2026-10-29", "2026-02-30T12:00:00Z", "2026-10-29T24:00:00Z", "2026-10-29T12:00:00"]) {
    const f = fixture();
    f.data.health.collection.last_completed_at = value;
    assert.equal((await inspectHealth(f.env, now)).status, "unknown", String(value));
  }
  for (const success of [false, undefined, "true"]) {
    const f = fixture();
    f.data.health.collection.success = success;
    assert.notEqual((await inspectHealth(f.env, now)).status, "healthy");
  }
});

test("audio date rolls at 22 Shanghai and across month/year boundaries", async () => {
  for (const instant of ["2026-10-29T13:59:59Z", "2026-01-01T00:00:00Z"]) {
    const at = Date.parse(instant);
    const f = fixture(at);
    const yesterday = shanghaiTime(at - 86_400_000).slice(0, 10);
    f.data.audio.date = yesterday;
    f.data.audio.audio_url = `/audio/${yesterday}/daily.mp3`;
    assert.equal((await inspectHealth(f.env, at)).status, "healthy");
  }
  const f = fixture();
  f.data.audio.date = "2026-10-28";
  f.data.audio.audio_url = "/audio/2026-10-28/daily.mp3";
  assert.equal((await inspectHealth(f.env, now)).issues.audio_stale, "unhealthy");
});

test("invalid, future and missing audio metadata, unsafe/mismatched paths", async () => {
  for (const patch of [
    { date: "2026-10-30" }, { date: "2026-02-30" }, { date: null },
    { generated_at: undefined }, { generated_at: "bad" }, { generated_at: new Date(now + 1).toISOString() },
    { audio_url: "" }, { audio_url: "https://external.invalid/private" },
    { audio_url: "/audio/../private.mp3" }, { audio_url: "/audio/%2e%2e/private.mp3" },
    { audio_url: "/audio/2026-10-28/daily.mp3" },
  ]) {
    const f = fixture();
    Object.assign(f.data.audio, patch);
    assert.notEqual((await inspectHealth(f.env, now)).status, "healthy", JSON.stringify(patch));
  }
});

test("metadata HTTP/JSON failures and audio MIME/byte/range failures", async () => {
  for (const patch of [
    { healthError: true }, { healthResponse: () => new Response("oops", { status: 404 }) },
    { healthResponse: () => new Response("<html>fallback</html>") }, { health: null },
    { audioResponse: () => Response.json([]) }, { audioResponse: () => new Response("oops", { status: 500 }) },
    { resourceError: true }, { bytes: new Uint8Array() }, { type: "text/html" },
    { status: 404 }, { range: "bad" },
  ]) {
    const f = fixture();
    Object.assign(f.data, patch);
    assert.notEqual((await inspectHealth(f.env, now)).status, "healthy");
  }
  const f = fixture();
  f.data.status = 200; // ASSETS can ignore Range; read a bounded sample.
  assert.equal((await inspectHealth(f.env, now)).status, "healthy");
});

test("signed Card 2.0, dedup, new issues, worsening, 6h repeat and recovery", async () => {
  requests = [];
  const start = now;
  const f = fixture();
  f.data.healthError = true;
  await scheduled({}, f.env);
  assert.equal(requests.length, 1);
  const payload = requests[0];
  assert.equal(payload.sign, createHmac("sha256", `${payload.timestamp}\ntest-secret`).update("").digest("base64"));
  assert.equal(payload.msg_type, "interactive");
  assert.equal(payload.card.schema, "2.0");
  assert.equal(payload.card.header.template, "red");
  assert.doesNotMatch(JSON.stringify(payload.card), /https?:|test-secret|secret-token/);
  await scheduled({}, f.env);
  assert.equal(requests.length, 1);
  f.data.resourceError = true; // new issue
  await scheduled({}, f.env);
  assert.equal(requests.length, 2);
  f.data.resourceError = false;
  f.data.type = "text/html"; // same issue worsens from unknown to unhealthy
  await scheduled({}, f.env);
  assert.equal(requests.length, 3);
  now = start + 6 * 3_600_000 - 1;
  await scheduled({}, f.env);
  assert.equal(requests.length, 3);
  now += 1;
  await scheduled({}, f.env);
  assert.equal(requests.length, 4);
  f.data.healthError = false;
  f.data.health.collection.last_completed_at = new Date(now).toISOString();
  f.data.type = "audio/mpeg";
  await scheduled({}, f.env);
  assert.equal(requests.length, 5);
  assert.equal(requests.at(-1).card.header.template, "green");
  await scheduled({}, f.env);
  assert.equal(requests.length, 5);
  now = start;
});

test("configuration, KV and notification failures are visible, sanitized and never claimed delivered", async () => {
  for (const missing of ["HEALTH_MONITOR_STATE", "HEALTH_WEBHOOK_URL", "HEALTH_WEBHOOK_SECRET"]) {
    const f = fixture();
    delete f.env[missing];
    await assert.rejects(scheduled({}, f.env), /missing/);
    assert.equal(f.storage.size, 0);
  }
  for (const mode of ["throw", "http", "json", "reject"]) {
    const f = fixture();
    f.data.healthError = true;
    delivery = mode;
    await assert.rejects(scheduled({}, f.env), /notification delivery failed/);
    assert.equal(f.storage.size, 0);
  }
  delivery = "success";
  for (const raw of ["bad", "null", "{}", '{"version":1,"status":"healthy","issues":{},"alert_open":true}']) {
    const f = fixture();
    f.storage.set(STATE_KEY, raw);
    await assert.rejects(scheduled({}, f.env), /KV state read failed or invalid/);
  }
  const f = fixture();
  f.env.HEALTH_MONITOR_STATE.get = async () => { throw new Error("private URL"); };
  await assert.rejects(scheduled({}, f.env), /KV state read failed/);
  f.env.HEALTH_MONITOR_STATE.get = async () => null;
  f.env.HEALTH_MONITOR_STATE.put = async () => { throw new Error("private URL"); };
  await assert.rejects(scheduled({}, f.env), /KV state write failed/);
  assert.doesNotMatch(logs.join("\n"), /https?:|secret-token|test-secret|private URL/);
});

test("failed recovery preserves the prior alert and retries on the next check", async () => {
  requests = [];
  const f = fixture();
  f.data.healthError = true;
  await scheduled({}, f.env);
  const before = f.storage.get(STATE_KEY);
  f.data.healthError = false;
  delivery = "reject";
  await assert.rejects(scheduled({}, f.env), /notification delivery failed/);
  assert.equal(f.storage.get(STATE_KEY), before);
  delivery = "success";
  await scheduled({}, f.env);
  assert.equal(JSON.parse(f.storage.get(STATE_KEY)).alert_open, false);
  assert.equal(requests.length, 3);
});

test('authenticated cloud activation sends one real-path card; repeated activation stays silent', async () => {
  requests = [];
  const f = fixture();
  f.env.PREVIEW_PASSWORD = 'fixture-password';
  const url = 'https://preview.newsradar.ccwu.cc/cloud-health/check';
  assert.equal((await worker.fetch(new Request(url, {method:'POST'}), f.env)).status, 401);
  const authorization = 'Basic ' + btoa('newsradar:fixture-password');
  assert.equal((await worker.fetch(new Request(url, {method:'POST', headers:{Authorization:authorization}}), f.env)).status, 403);
  for (let count = 0; count < 2; count++) {
    const response = await worker.fetch(new Request(url, {method:'POST', headers:{Authorization:authorization, 'X-NewsRadar-Health-Check':'1'}}), f.env);
    assert.equal(response.status, 200);
    assert.equal((await response.json()).success, true);
    assert.equal(requests.length, 1);
  }
  assert.equal(requests[0].card.header.template, 'green');
  assert.match(requests[0].card.header.title.content, /正常/);
});
