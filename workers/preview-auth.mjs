import { scheduled, inspectHealth } from "./dashboard-monitor.mjs";

const USERNAME = "newsradar";

function unauthorized() {
  return new Response("NewsRadar preview authentication required.\n", {
    status: 401,
    headers: {
      "Cache-Control": "no-store",
      "Content-Type": "text/plain; charset=utf-8",
      "WWW-Authenticate": 'Basic realm="NewsRadar Preview", charset="UTF-8"',
      "X-Robots-Tag": "noindex, nofollow, noarchive",
    },
  });
}

async function secureEqual(actual, expected) {
  const encoder = new TextEncoder();
  const [actualHash, expectedHash] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(actual)),
    crypto.subtle.digest("SHA-256", encoder.encode(expected)),
  ]);
  const left = new Uint8Array(actualHash);
  const right = new Uint8Array(expectedHash);
  let difference = left.length ^ right.length;
  for (let index = 0; index < Math.min(left.length, right.length); index += 1) {
    difference |= left[index] ^ right[index];
  }
  return difference === 0;
}

function parseRange(value, size) {
  const match = /^bytes=(\d*)-(\d*)$/.exec(value || "");
  if (!match || (!match[1] && !match[2]) || size <= 0) return null;
  let start;
  let end;
  if (!match[1]) {
    const suffixLength = Number(match[2]);
    if (!Number.isSafeInteger(suffixLength) || suffixLength <= 0) return null;
    start = Math.max(0, size - suffixLength);
    end = size - 1;
  } else {
    start = Number(match[1]);
    end = match[2] ? Number(match[2]) : size - 1;
    if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end)) return null;
  }
  if (start < 0 || start >= size || end < start) return null;
  return { start, end: Math.min(end, size - 1) };
}

async function withByteRange(request, response) {
  const rangeHeader = request.headers.get("Range");
  if (!rangeHeader || request.method !== "GET" || response.status !== 200) return response;
  const bytes = await response.arrayBuffer();
  const range = parseRange(rangeHeader, bytes.byteLength);
  if (!range) {
    return new Response(null, {
      status: 416,
      headers: {
        "Accept-Ranges": "bytes",
        "Content-Range": `bytes */${bytes.byteLength}`,
      },
    });
  }
  const headers = new Headers(response.headers);
  headers.set("Accept-Ranges", "bytes");
  headers.set("Content-Length", String(range.end - range.start + 1));
  headers.set("Content-Range", `bytes ${range.start}-${range.end}/${bytes.byteLength}`);
  return new Response(bytes.slice(range.start, range.end + 1), {
    status: 206,
    headers,
  });
}

export default {
  scheduled,
  async fetch(request, env) {
    if (!env.PREVIEW_PASSWORD) {
      return new Response("Preview authentication is not configured.\n", {
        status: 503,
        headers: { "Cache-Control": "no-store" },
      });
    }

    const actual = request.headers.get("Authorization") || "";
    const expected = `Basic ${btoa(`${USERNAME}:${env.PREVIEW_PASSWORD}`)}`;
    if (!(await secureEqual(actual, expected))) {
      return unauthorized();
    }

    // Explicit authenticated action; custom header prevents cross-site form triggers.
    if (new URL(request.url).pathname === '/cloud-health/check' && request.method === 'POST') {
      if (request.headers.get('X-NewsRadar-Health-Check') !== '1') {
        return new Response('Explicit health check header required', { status: 403 });
      }
      await scheduled({ cron: 'activation' }, env);
      return Response.json({ success: true,
        state: await env.HEALTH_MONITOR_STATE.get('dashboard-monitor:v1', 'json') },
        { headers: { 'Cache-Control': 'no-store' } });
    }

    if (new URL(request.url).pathname === '/cloud-health.json' && request.method === 'GET') {
      const report = await inspectHealth(env);
      return Response.json({ ...report, scheduler_enabled: env.HEALTH_MONITOR_ENABLED === 'true',
        last_scheduled_check: await env.HEALTH_MONITOR_STATE?.get('dashboard-monitor:v1', 'json') ?? null },
        { headers: { 'Cache-Control': 'no-store', 'X-Robots-Tag': 'noindex, nofollow' } });
    }

    return withByteRange(request, await env.ASSETS.fetch(request));
  },
};

export { parseRange, withByteRange };
