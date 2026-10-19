import assert from "node:assert/strict";
import worker from "../workers/preview-auth.mjs";

const assets = {
  fetch: async () => new Response("preview ok", { status: 200 }),
};

const denied = await worker.fetch(new Request("https://preview.example.test/"), {
  ASSETS: assets,
  PREVIEW_PASSWORD: "test-password",
});
assert.equal(denied.status, 401);
assert.match(denied.headers.get("www-authenticate"), /NewsRadar Preview/);
assert.match(denied.headers.get("x-robots-tag"), /noindex/);

const credentials = btoa("newsradar:test-password");
const allowed = await worker.fetch(
  new Request("https://preview.example.test/", {
    headers: { Authorization: `Basic ${credentials}` },
  }),
  { ASSETS: assets, PREVIEW_PASSWORD: "test-password" },
);
assert.equal(allowed.status, 200);
assert.equal(await allowed.text(), "preview ok");

const unconfigured = await worker.fetch(new Request("https://preview.example.test/"), {
  ASSETS: assets,
});
assert.equal(unconfigured.status, 503);

const mediaAssets = {
  fetch: async () => new Response(new Uint8Array([0, 1, 2, 3, 4, 5]), {
    status: 200,
    headers: { "Content-Type": "audio/mpeg" },
  }),
};
async function ranged(range) {
  return worker.fetch(
    new Request("https://preview.example.test/audio/test.mp3", {
      headers: { Authorization: `Basic ${credentials}`, Range: range },
    }),
    { ASSETS: mediaAssets, PREVIEW_PASSWORD: "test-password" },
  );
}

const partial = await ranged("bytes=2-4");
assert.equal(partial.status, 206);
assert.equal(partial.headers.get("content-range"), "bytes 2-4/6");
assert.equal(partial.headers.get("accept-ranges"), "bytes");
assert.deepEqual(Array.from(new Uint8Array(await partial.arrayBuffer())), [2, 3, 4]);

const suffix = await ranged("bytes=-2");
assert.equal(suffix.status, 206);
assert.equal(suffix.headers.get("content-range"), "bytes 4-5/6");
assert.deepEqual(Array.from(new Uint8Array(await suffix.arrayBuffer())), [4, 5]);

const unsatisfiable = await ranged("bytes=20-30");
assert.equal(unsatisfiable.status, 416);
assert.equal(unsatisfiable.headers.get("content-range"), "bytes */6");
