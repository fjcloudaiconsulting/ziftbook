// ZIF-73: the admin's businesses list and detail. node --test can't render the screens (no DOM or React renderer),
// so this checks the plain logic and, loosely, what the screens read.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

import { needsSignIn, pageCount, settingText, shortDate, writeStep } from "../lib/admin.ts";
import { dateLocale } from "../lib/console.ts";

const read = (path) => readFileSync(new URL(`../app/[locale]/admin/${path}`, import.meta.url), "utf8");
const list = read("businesses/businesses.tsx");
const detail = read("businesses/[id]/business.tsx");
const suspension = read("businesses/[id]/suspension.tsx");
const signInForm = readFileSync(new URL("../app/[locale]/sign-in/sign-in-form.tsx", import.meta.url), "utf8");

describe("signed out or short of the second factor", () => {
  // ZIF-73 fence. Kills: a half session (403) or an ended one (401) left on an empty businesses screen instead of
  // sent back through sign-in, which resumes at the right step.
  test("401 and 403 go back through sign-in, nothing else does", () => {
    assert.equal(needsSignIn(401), true);
    assert.equal(needsSignIn(403), true);
    for (const status of [0, 200, 404, 422, 500, 503]) assert.equal(needsSignIn(status), false);
    for (const screen of [list, detail]) assert.match(screen, /needsSignIn\((shown\.)?outcome\.status\)\) router\.replace\("\/admin\/sign-in"\)/);
  });
});

describe("answers in flight", () => {
  // ZIF-73 fence. Kills: the detail fetched again when the router or a translator changes (each GET writes a
  // business_read), and a stale answer (an earlier search or id) overwriting the newer one.
  test("the detail fetches on its id alone; both screens drop a stale answer", () => {
    assert.match(detail, /\}, \[id, reloads\]\);/); // no router or translator: a refetch follows only the admin's own change
    for (const screen of [list, detail]) assert.match(screen, /if \(current\) set/);
  });

  // ZIF-73 fence. Kills: a failed page fetch leaving the previous page's rows under the new page number.
  test("the list keeps rows and page number in one answer", () => {
    assert.match(list, /setShown\(\{ query, outcome \}\)/);
    assert.match(list, /const page = shown\?\.query\.page/);
    assert.doesNotMatch(list, /[^.]query\.page/); // only shown?.query.page names the page shown
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
    const day = new Intl.DateTimeFormat(dateLocale("en"), { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
    assert.equal(shortDate(null, day), "—");
    assert.equal(shortDate("2026-11-08T12:00:00Z", day), "8 Nov 2026");
  });
});

describe("no client data", () => {
  // ZIF-73 fence. Kills: a client's field read on the admin screens before the audited reveal (a later PR) exists.
  test("the screens read no client, booking, email, phone or address field", () => {
    for (const screen of [list, detail]) assert.doesNotMatch(screen, /client_|\.email|phone|\.ip\b|user_agent|booking/i);
  });
});

const NEW_KEYS = [
  "statusLabel", "active", "suspendedSince", "suspended", "reasonLabel", "suspendAction", "reactivateAction", "suspendTitle",
  "reactivateTitle", "suspendBody", "suspendHint", "reactivateHint", "reasonRequired", "reasonInvalid", "recheckTitle",
  "recheckLede", "suspendedDone", "reactivatedDone", "changedNote", "upcomingTitle", "upcomingSome", "upcomingNone",
  "upcomingZone", "keepRunning", "keepSuspended",
];

describe("copy", () => {
  // ZIF-73 fence. Kills: a key the screens use missing in a language (check-catalogs compares the catalogs with each
  // other, not with the code).
  const keys = [...new Set([...`${list}\n${detail}\n${suspension}`.matchAll(/\bt\("([A-Za-z.]+)"/g)].map((match) => match[1]))];
  for (const locale of ["en", "nl", "pt"]) {
    test(`every key the screens use is in ${locale}`, () => {
      const { Admin, SignIn } = JSON.parse(readFileSync(new URL(`../messages/${locale}.json`, import.meta.url), "utf8"));
      for (const key of keys) assert.ok(key.split(".").reduce((node, part) => node?.[part], Admin), `${locale}: Admin.${key}`);
      for (const key of NEW_KEYS) assert.ok(Admin[key], `${locale}: Admin.${key}`);
      assert.ok(SignIn.suspended, `${locale}: SignIn.suspended`);
      assert.ok(!/—/.test(JSON.stringify([NEW_KEYS.map((key) => Admin[key]), SignIn.suspended])), `${locale}: no em dash`);
      for (const group of ["stripe.none", "stripe.onboarding", "stripe.ready", "role.owner", "role.worker"]) {
        assert.ok(group.split(".").reduce((node, part) => node?.[part], Admin), `${locale}: Admin.${group}`);
      }
    });
  }
});

describe("a write's answer (writeStep)", () => {
  // ZIF-73 fence F1. Kills: writeStep delegating to needsSignIn (a stale factor sent to /admin/sign-in, which replaces
  // straight back to /admin: a redirect loop, and the typed reason lost).
  test("a stale or missing second factor is a re-check, not a sign-in", () => {
    assert.equal(writeStep(403, "second_factor_stale"), "recheck");
    assert.equal(writeStep(403, "second_factor_required"), "recheck");
  });

  // ZIF-73 fence F2. Kills: every 401/403 read as a re-check (a code prompt on a dead session, a loop on an unrelated 403).
  test("a dead session or any other 403 goes back through sign-in", () => {
    assert.equal(writeStep(401), "signIn");
    assert.equal(writeStep(403, "forbidden"), "signIn");
    assert.equal(writeStep(403), "signIn");
  });

  // ZIF-73 fence F3. Kills: a 204 (no body) read as a failure, and a 409 shown as a generic error.
  test("204 is done and a 409 means someone changed it first", () => {
    assert.equal(writeStep(204), "done");
    assert.equal(writeStep(409, "already_suspended"), "changed");
    assert.equal(writeStep(409, "not_suspended"), "changed");
  });

  test("404, 422 and the rest", () => {
    assert.equal(writeStep(404), "notFound");
    assert.equal(writeStep(422), "invalid");
    assert.equal(writeStep(503), "other");
    assert.equal(writeStep(0), "other");
  });
});

describe("what the suspension screens read", () => {
  // ZIF-73 fences F6, F3b. Kills: the re-check bypassed (needsSignIn on a write), a write's 204 read as data.
  test("the panel sends through writeStep and the code form, never needsSignIn, never a body", () => {
    assert.match(suspension, /writeStep\(/);
    assert.match(suspension, /adminPassSecondFactor\(/);
    assert.doesNotMatch(suspension, /needsSignIn\(/);
    assert.match(suspension, /writeStep\(\w+\.status, \w+\.code\)/);
    assert.doesNotMatch(suspension, /\.data\b/);
  });

  // ZIF-73 fences F6, F6b. Kills: no refetch after a change, browser-zone times, "[object Object]" for a service,
  // a second formatter without the business's zone.
  test("the detail shows times in the business's zone and a service by its name", () => {
    assert.match(detail, /\}, \[id, reloads\]\);/);
    assert.match(detail, /serviceName\(/);
    // the only formatRange call is on the formatter that has the business's zone
    assert.equal(detail.split("formatRange(").length - 1, 1);
    assert.match(detail, /const range =\s+business && new Intl\.DateTimeFormat\([^\n]*timeZone: business\.settings\.timezone/);
    assert.match(detail, /range\.formatRange\(/);
    assert.equal(detail.split("timeZone: business.settings.timezone").length - 1, 1);
  });

  // ZIF-73 fence F6. Kills: no pill on a suspended row.
  test("a suspended row carries the warning pill", () => {
    assert.match(list, /suspended_at/);
    assert.match(list, /pillWarn/);
  });

  // ZIF-73 fence F6. Kills: the suspended branch after the 403 noBusiness one (never reached).
  test("sign-in reads a suspended business before any other 403", () => {
    const suspended = signInForm.indexOf('meaning === "suspended"');
    assert.ok(suspended > -1);
    assert.ok(suspended < signInForm.indexOf("outcome.status === 403"));
  });
});
