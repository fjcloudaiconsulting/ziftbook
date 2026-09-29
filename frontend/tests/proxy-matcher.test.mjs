// F7: which paths the locale-routing matcher catches. A bare public slug like /apiary must not be
// swallowed by a prefix check meant only for /api itself.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

import { LOCALE_MATCHER } from "../lib/upstream.ts";

const localeRegex = new RegExp(`^${LOCALE_MATCHER}$`);

describe("proxy matcher (F7)", () => {
  test("fence: /apiary matches the locale route, /api and /api/x do not (kills the prefix regex)", () => {
    // Wrong implementation killed: `(?!api|_next|_vercel|.*\\..*)` (no boundary), which refuses
    // every path merely starting with "api", including a real business slug "apiary".
    assert.match("/apiary", localeRegex);
    assert.doesNotMatch("/api", localeRegex);
    assert.doesNotMatch("/api/x", localeRegex);
  });

  test("Next internals and files with an extension are still skipped", () => {
    assert.doesNotMatch("/_next/static/x.js", localeRegex);
    assert.doesNotMatch("/_vercel/insights", localeRegex);
    assert.doesNotMatch("/favicon.ico", localeRegex);
  });

  test("an ordinary locale path still matches", () => {
    assert.match("/en/booking", localeRegex);
    assert.match("/a-2", localeRegex);
  });

  test("guard: proxy.ts's static matcher literal (Next can't parse an imported one) matches LOCALE_MATCHER", () => {
    const source = readFileSync(new URL("../proxy.ts", import.meta.url), "utf8");
    const match = /matcher:\s*\["\/api\/:path\*",\s*"((?:[^"\\]|\\.)*)"\]/.exec(source);
    assert.ok(match, "matcher literal not found in proxy.ts");
    assert.equal(eval(`"${match[1]}"`), LOCALE_MATCHER);
  });
});
