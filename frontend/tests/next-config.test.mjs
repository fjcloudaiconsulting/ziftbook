import assert from "node:assert/strict";
import { test } from "node:test";
import config from "../next.config.ts";

test("the image optimizer is off (ZB-34: no /_next/image, no sharp at runtime)", () => {
  assert.equal(config.images?.unoptimized, true);
});

const PAGES = "/((?!api/).*)";

async function pageHeaders() {
  const rules = await config.headers();
  const rule = rules.find((r) => r.source === PAGES);
  assert.ok(rule, "a header rule covers every non-API path");
  return Object.fromEntries(rule.headers.map((h) => [h.key, h.value]));
}

test("every page is sent over HTTPS only, never sniffed, and keeps popups and payment scoped", async () => {
  const headers = await pageHeaders();
  assert.equal(headers["Strict-Transport-Security"], "max-age=31536000; includeSubDomains");
  assert.equal(headers["X-Content-Type-Options"], "nosniff");
  assert.equal(
    headers["Permissions-Policy"],
    'camera=(), microphone=(), geolocation=(), payment=(self "https://js.stripe.com")',
  );
  assert.equal(headers["Cross-Origin-Opener-Policy"], "same-origin-allow-popups");
});

test("every page keeps its referrer and framing policies", async () => {
  const headers = await pageHeaders();
  assert.equal(headers["Referrer-Policy"], "no-referrer");
  assert.equal(headers["Content-Security-Policy"], "frame-ancestors 'none'");
});

test("the framework does not announce itself", () => {
  assert.equal(config.poweredByHeader, false);
});
