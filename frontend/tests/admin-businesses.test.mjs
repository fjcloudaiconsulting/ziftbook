// ZIF-73: the admin's businesses list and detail. node --test can't render the screens (no DOM or React renderer),
// so this checks the plain logic and, loosely, what the screens read.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

import { needsSignIn, pageCount, settingText, shortDate } from "../lib/admin.ts";

const read = (path) => readFileSync(new URL(`../app/[locale]/admin/${path}`, import.meta.url), "utf8");
const list = read("businesses/businesses.tsx");
const detail = read("businesses/[id]/business.tsx");

describe("signed out or short of the second factor", () => {
  // ZIF-73 fence. Kills: a half session (403) or an ended one (401) left on an empty businesses screen instead of
  // sent back through sign-in, which resumes at the right step.
  test("401 and 403 go back through sign-in, nothing else does", () => {
    assert.equal(needsSignIn(401), true);
    assert.equal(needsSignIn(403), true);
    for (const status of [0, 200, 404, 422, 500, 503]) assert.equal(needsSignIn(status), false);
    for (const screen of [list, detail]) assert.match(screen, /needsSignIn\(outcome\.status\)\) router\.replace\("\/admin\/sign-in"\)/);
  });
});

describe("pages", () => {
  // ZIF-73 fence. Kills: a pager that offers an empty last page, or none for an empty search.
  test("whole pages, at least one", () => {
    assert.equal(pageCount(0, 25), 1);
    assert.equal(pageCount(25, 25), 1);
    assert.equal(pageCount(26, 25), 2);
  });
});

describe("settings and dates", () => {
  test("switches read yes or no, nothing reads as a dash", () => {
    assert.equal(settingText(true, "Yes", "No"), "Yes");
    assert.equal(settingText(false, "Yes", "No"), "No");
    assert.equal(settingText("", "Yes", "No"), "—");
    assert.equal(settingText(null, "Yes", "No"), "—");
    assert.equal(settingText(0, "Yes", "No"), "0");
    assert.equal(settingText("Europe/Lisbon", "Yes", "No"), "Europe/Lisbon");
  });

  test("a date the API doesn't have is a dash; en reads day first", () => {
    assert.equal(shortDate(null, "en"), "—");
    assert.match(shortDate("2026-11-08T12:00:00Z", "en"), /^8 Nov 2026$/);
  });
});

describe("no client data", () => {
  // ZIF-73 fence. Kills: a client's field read on the admin screens before the audited reveal (a later PR) exists.
  test("the screens read no client, booking, email, phone or address field", () => {
    for (const screen of [list, detail]) assert.doesNotMatch(screen, /client_|\.email|phone|\.ip\b|user_agent|booking/i);
  });
});

describe("copy", () => {
  // ZIF-73 fence. Kills: a key the screens use missing in a language (check-catalogs compares the catalogs with each
  // other, not with the code).
  const keys = [...new Set([...`${list}\n${detail}`.matchAll(/\bt\("([A-Za-z.]+)"/g)].map((match) => match[1]))];
  for (const locale of ["en", "nl", "pt"]) {
    test(`every key the screens use is in ${locale}`, () => {
      const { Admin } = JSON.parse(readFileSync(new URL(`../messages/${locale}.json`, import.meta.url), "utf8"));
      for (const key of keys) assert.ok(key.split(".").reduce((node, part) => node?.[part], Admin), `${locale}: Admin.${key}`);
      for (const group of ["stripe.none", "stripe.onboarding", "stripe.ready", "role.owner", "role.worker"]) {
        assert.ok(group.split(".").reduce((node, part) => node?.[part], Admin), `${locale}: Admin.${group}`);
      }
    });
  }
});
