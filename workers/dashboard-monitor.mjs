// Independent of the publishing machine: inspect the deployed asset binding only.
const ORIGIN = "https://preview.newsradar.ccwu.cc";
export const STATE_KEY = "dashboard-monitor:v1";
const MINUTE = 60_000;
const REPEAT_MS = 6 * 60 * MINUTE;
const rank = { healthy: 0, unknown: 1, unhealthy: 2 };

function validDay(value) {
  return typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value) &&
    Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value;
}

function timestamp(value) {
  if (typeof value !== "string" ||
      !/^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$/.test(value) ||
      !validDay(value.slice(0, 10))) return null;
  const result = Date.parse(value);
  return Number.isFinite(result) ? result : null;
}

export function shanghaiTime(value) {
  return Number.isFinite(value)
    ? new Date(value + 8 * 60 * MINUTE).toISOString().slice(0, 19).replace("T", " ")
    : "未知";
}

async function asset(env, path, consume, headers = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15_000);
  try {
    const response = await env.ASSETS.fetch(new Request(ORIGIN + path, {
      headers, signal: controller.signal, redirect: "manual",
    }));
    return await consume(response);
  } finally {
    clearTimeout(timeout);
  }
}

async function readJSON(env, path) {
  return asset(env, path, async response => {
    if (response.status !== 200) throw new Error("asset_status:" + response.status);
    const data = await response.json();
    if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("invalid metadata");
    return data;
  });
}

export async function inspectHealth(env, now = Date.now()) {
  const issues = {};
  const diagnostics = {};
  const details = [];
  const add = (key, status, text) => { issues[key] = status; details.push(text); };
  const local = shanghaiTime(now);
  const today = local.slice(0, 10);
  const requiredDay = Number(local.slice(11, 13)) >= 22 ? today
    : shanghaiTime(now - 24 * 60 * MINUTE).slice(0, 10);
  const [health, audio] = await Promise.allSettled([
    readJSON(env, "/health.json"), readJSON(env, "/audio/latest.json"),
  ]);
  if (health.status === "rejected") {
    diagnostics.collection = health.reason?.message?.startsWith("asset_status:") ? health.reason.message : "asset_read_failed";
    add("collection_metadata", "unknown", "采集：未知（健康元数据不可用）");
  } else {
    const collection = health.value.collection;
    const completed = timestamp(collection?.last_completed_at);
    if (completed === null || completed - now > 5 * MINUTE) {
      add("collection_time", "unknown", "采集：未知（完成时间缺失、无效或超前超过 5 分钟）");
    } else {
      const age = Math.max(0, Math.round((now - completed) / MINUTE));
      details.push(`上次采集完成：${shanghaiTime(completed)}；距今 ${age} 分钟`);
      if (now - completed > 120 * MINUTE) add("collection_stale", "unhealthy", "采集：异常（超过 120 分钟）");
    }
    if (collection?.success === false) add("collection_failed", "unhealthy", "采集：异常（最近一次采集失败）");
    else if (collection?.success !== true) add("collection_result", "unknown", "采集：未知（缺少有效成功标记）");
  }
  details.push(`音频要求日期：${requiredDay}（上海时间 22:00 起要求当天）`);
  if (audio.status === "rejected") {
    diagnostics.audio = audio.reason?.message?.startsWith("asset_status:") ? audio.reason.message : "asset_read_failed";
    add("audio_metadata", "unknown", "音频：未知（元数据不可用）");
  } else {
    const metadata = audio.value;
    if (!validDay(metadata.date) || metadata.date > today) {
      add("audio_date", "unknown", "音频：未知（日期无效或在未来）");
    } else {
      details.push(`音频日期：${metadata.date}`);
      if (metadata.date < requiredDay) add("audio_stale", "unhealthy", "音频：异常（未达到要求日期）");
    }
    const generated = timestamp(metadata.generated_at);
    if (generated === null || generated > now) {
      add("audio_generated", "unknown", "音频：未知（生成时间缺失、无效或在未来）");
    } else {
      details.push(`音频生成：${shanghaiTime(generated)}`);
    }
    // Only local, literal asset paths. Never follow metadata to another host.
    const path = metadata.audio_url;
    if (typeof path !== "string" || !/^\/audio\/[A-Za-z0-9_/-]+\.[A-Za-z0-9]+$/.test(path) ||
        !validDay(metadata.date) || !path.startsWith(`/audio/${metadata.date}/`)) {
      add("audio_path", "unknown", "音频：未知（资源路径缺失或无效）");
    } else {
      try {
        const size = await asset(env, path, async response => {
          try {
            if (![200, 206].includes(response.status) ||
                !/^audio\/[\w.+-]+(?:\s*;|$)/i.test(response.headers.get("Content-Type") || "")) return 0;
            if (response.status === 206 && !/^bytes 0-\d+\/[1-9]\d*$/.test(response.headers.get("Content-Range") || "")) return 0;
            const reader = response.body?.getReader();
            if (!reader) return 0;
            try {
              for (;;) {
                const { done, value } = await reader.read();
                if (done) return 0;
                if (value?.byteLength) return Math.min(32, value.byteLength);
              }
            } finally { await reader.cancel(); }
          } finally { if (response.body && !response.body.locked) await response.body.cancel(); }
        }, { Range: "bytes=0-31" });
        if (!size) add("audio_resource", "unhealthy", "音频：异常（资源无有效音频类型或无字节）");
        else details.push(`音频资源：已验证，读取 ${size} 字节`);
      } catch {
        add("audio_resource", "unknown", "音频：未知（资源读取失败）");
      }
    }
  }
  const status = Object.values(issues).reduce((result, item) => rank[item] > rank[result] ? item : result, "healthy");
  return { status, issues, details, checked_at: now, diagnostics };
}

export function buildCard(result, recovery = false) {
  const label = recovery ? "已恢复" : result.status === "healthy" ? "正常" : result.status === "unhealthy" ? "异常" : "未知";
  return {
    schema: "2.0",
    config: { update_multi: true },
    header: { template: recovery || result.status === "healthy" ? "green" : "red", title: { tag: "plain_text", content: `NewsRadar 云端健康 · ${label}` } },
    body: { elements: [{ tag: "markdown", content:
      `**检查时间：${shanghaiTime(result.checked_at)}（上海）**\n${result.details.join("\n")}` }] },
  };
}

async function sendCard(env, result, recovery, now) {
  const seconds = String(Math.floor(now / 1000));
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(`${seconds}\n${env.HEALTH_WEBHOOK_SECRET}`),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const signature = await crypto.subtle.sign("HMAC", key, new Uint8Array());
  const sign = btoa(String.fromCharCode(...new Uint8Array(signature)));
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15_000);
  try {
    const response = await fetch(env.HEALTH_WEBHOOK_URL, {
      method: "POST", redirect: "manual", signal: controller.signal,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ timestamp: seconds, sign, msg_type: "interactive", card: buildCard(result, recovery) }),
    });
    if (!response.ok || (await response.json())?.code !== 0) throw new Error("delivery failed");
  } finally { clearTimeout(timeout); }
}

export async function scheduled(_controller, env) {
  // Deliberately replace all external errors: they can contain webhook secrets/URLs.
  const fail = reason => { console.error(`[cloud-monitor] ${reason}`); throw new Error(`[cloud-monitor] ${reason}`); };
  if (!env.HEALTH_MONITOR_STATE?.get || !env.HEALTH_MONITOR_STATE?.put) fail("KV binding missing");
  if (!env.HEALTH_WEBHOOK_URL || !env.HEALTH_WEBHOOK_SECRET) fail("dedicated webhook configuration missing");
  const now = Date.now();
  let previous;
  try {
    const raw = await env.HEALTH_MONITOR_STATE.get(STATE_KEY);
    previous = raw === null ? null : JSON.parse(raw);
    if (raw !== null && (previous?.version !== 1 || !Object.hasOwn(rank, previous.status) ||
        !previous.issues || typeof previous.issues !== "object" || Array.isArray(previous.issues) ||
        Object.values(previous.issues).some(value => !["unknown", "unhealthy"].includes(value)) ||
        typeof previous.alert_open !== "boolean" ||
        (previous.alert_open && (!Number.isFinite(previous.last_alert_at) || previous.last_alert_at > now)))) {
      throw new Error("invalid state");
    }
  } catch { fail("KV state read failed or invalid"); }
  const result = await inspectHealth(env, now);
  const recovery = result.status === "healthy" && previous?.alert_open === true;
  const changed = Object.entries(result.issues).some(([key, value]) => rank[value] > (rank[previous?.issues?.[key]] ?? 0));
  const alert = result.status !== "healthy" && (!previous?.alert_open || changed || now - previous.last_alert_at >= REPEAT_MS);
  const activation = _controller?.cron === "activation" && previous === null;
  if (alert || recovery || activation) {
    try { await sendCard(env, result, recovery, now); }
    catch { fail("notification delivery failed; state not advanced"); }
  }
  const next = { version: 1, ...result, alert_open: result.status !== "healthy" && (alert || previous?.alert_open === true),
    last_alert_at: alert ? now : previous?.last_alert_at ?? null };
  try { await env.HEALTH_MONITOR_STATE.put(STATE_KEY, JSON.stringify(next)); }
  catch { fail("KV state write failed; delivery deduplication not guaranteed"); }
  console.log(`[cloud-monitor] status=${result.status} notification=${alert || recovery || activation ? "delivered" : "silent"}`);
}
