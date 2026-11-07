// The contact email confirm page (ZIF-232): which links are worth a button, what an answer means,
// and its copy.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

import { confirmOutcome, firstStage } from "../lib/contact-email.ts";

const TENANT = "0192f3a4-5b6c-7d8e-9f01-23456789abcd";
const SECRET = "Ab3_-".repeat(8) + "xyz"; // 43 URL-safe characters

describe("where the page starts", () => {
  test("no link asks for the email link; a mangled one is dead; a good one gets the button", () => {
    assert.equal(firstStage(null), "missing");
    assert.equal(firstStage(`${TENANT}.${SECRET}`), "ready");
    assert.equal(firstStage(`${TENANT}.${SECRET.slice(1)}`), "invalid");
    assert.equal(firstStage(SECRET), "invalid");
    assert.equal(firstStage("x".repeat(101)), "invalid");
  });
});

describe("what a confirm answer means", () => {
  test("each answer has its own screen, and anything else is a retry", () => {
    assert.equal(confirmOutcome(204), "done");
    assert.equal(confirmOutcome(400), "invalid");
    assert.equal(confirmOutcome(429), "tooMany");
    for (const status of [0, 415, 422, 500, 503]) assert.equal(confirmOutcome(status), "other");
  });
});

describe("the page", () => {
  const page = readFileSync(new URL("../app/[locale]/account/contact-email/confirm-contact-email.tsx", import.meta.url), "utf8");

  // F6.1 FENCE. Wrong impl: a token read from the query string, or one left in the address bar.
  test("reads the token from the fragment only, through takeToken", () => {
    assert.match(page, /takeToken\(window\)/);
    assert.doesNotMatch(page, /searchParams|location\.search|useSearchParams/);
  });

  // F6.2 FENCE. Wrong impl: confirming on load, which a scanner that runs scripts would do.
  test("confirms only from the button's handler", () => {
    const calls = [...page.matchAll(/contactEmailConfirm\(/g)];
    assert.equal(calls.length, 1);
    assert.doesNotMatch(page, /useEffect/);
  });
});

describe("the copy", () => {
  // F6.3. Wrong impl: a key missing in one language, or an em dash slipping in.
  for (const locale of ["en", "nl", "pt"]) {
    test(`${locale} has every key and no em dash`, () => {
      const messages = JSON.parse(readFileSync(new URL(`../messages/${locale}.json`, import.meta.url), "utf8"));
      const copy = messages.ContactEmail;
      assert.deepEqual(Object.keys(copy).sort(), ["confirm", "confirming", "done", "doneTitle", "intro", "invalid", "invalidTitle", "missing", "title", "tooMany"]);
      for (const [key, value] of Object.entries(copy)) assert.doesNotMatch(value, /—/, key);
    });
  }
});
