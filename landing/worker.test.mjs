import assert from "node:assert/strict";
import { test } from "node:test";

import worker, { pickLanguage } from "./worker.js";

test("picks the highest-weighted supported language", () => {
  assert.equal(pickLanguage("pt-BR,en;q=0.8"), "pt");
  assert.equal(pickLanguage("de,nl;q=0.5"), "nl");
  assert.equal(pickLanguage("en;q=0.2,nl;q=0.9"), "nl");
  assert.equal(pickLanguage("nl-BE"), "nl");
});

test("falls back to English", () => {
  assert.equal(pickLanguage(""), "en");
  assert.equal(pickLanguage(null), "en");
  assert.equal(pickLanguage("*"), "en");
  assert.equal(pickLanguage("de-DE,fr;q=0.9"), "en");
  assert.equal(pickLanguage("nl;q=0"), "en");
});

test("root redirects without caching and varies on language", async () => {
  const request = new Request("https://ziftbook.com/", { headers: { "Accept-Language": "pt-PT" } });
  const response = await worker.fetch(request, { ASSETS: { fetch: () => new Response("asset") } });
  assert.equal(response.status, 302);
  assert.equal(response.headers.get("Location"), "/pt/");
  assert.equal(response.headers.get("Cache-Control"), "no-store");
  assert.equal(response.headers.get("Vary"), "Accept-Language");
});

test("any other path is served from static assets", async () => {
  const response = await worker.fetch(new Request("https://ziftbook.com/nl/"), {
    ASSETS: { fetch: () => new Response("asset") },
  });
  assert.equal(await response.text(), "asset");
});
