// Pure logic for blocked time: local-time-to-instant conversion (DST correct,
// matching the server's own rule, no browser midnight math), the request body per kind, the PATCH
// change-set, the list window (today .. today+horizon), list labels, who may edit a block, and
// which fields a partial block's form needs before it can be converted at all.
//
// TZ is pinned to a zone well away from every zone under test (Sao Paulo, UTC-3, no DST) so a
// `new Date(...)` built from the runner's own local time - if one ever sneaks into this file or
// the code under test - reads differently than in the zones being tested, rather than by luck
// matching one of them and hiding a bug.
process.env.TZ = "America/Sao_Paulo";

import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { beyondHorizon, blockLabel, canEditBlock, clientProblem, listWindow, localToInstant, patchBody, requestBody } from "../lib/time-off.ts";

describe("localToInstant", () => {
  test("Amsterdam, before the spring-forward transition (2026-03-29), uses CET (+01:00)", () => {
    assert.equal(localToInstant("2026-03-29", "01:30", "Europe/Amsterdam"), "2026-03-29T00:30:00.000Z");
  });

  test("Amsterdam, after the spring-forward transition (2026-03-29), uses CEST (+02:00)", () => {
    assert.equal(localToInstant("2026-03-29", "03:30", "Europe/Amsterdam"), "2026-03-29T01:30:00.000Z");
  });

  test("Amsterdam, before the fall-back transition (2026-10-25), uses CEST (+02:00)", () => {
    assert.equal(localToInstant("2026-10-25", "01:00", "Europe/Amsterdam"), "2026-10-24T23:00:00.000Z");
  });

  test("Amsterdam, after the fall-back transition (2026-10-25), uses CET (+01:00)", () => {
    assert.equal(localToInstant("2026-10-25", "04:00", "Europe/Amsterdam"), "2026-10-25T03:00:00.000Z");
  });

  // The four cases below fall INSIDE a transition's gap or fold, and each expected value is
  // copied from the server's own conversion (backend `schedule.to_utc`, confirmed by running it
  // directly), not computed by hand - so a client/server mismatch here is exactly the bug that
  // would otherwise reach production.

  test("Amsterdam 2026-10-25 02:30 is a repeated (fall-back) wall-clock time: the FIRST occurrence, CEST, per schedule.to_utc", () => {
    assert.equal(localToInstant("2026-10-25", "02:30", "Europe/Amsterdam"), "2026-10-25T00:30:00.000Z");
  });

  test("New York 2026-03-08 02:30 is a skipped (spring-forward) wall-clock time: the offset from BEFORE the change (EST), per schedule.to_utc", () => {
    assert.equal(localToInstant("2026-03-08", "02:30", "America/New_York"), "2026-03-08T07:30:00.000Z");
  });

  test("Sydney 2026-04-05 02:30 is a repeated (fall-back) wall-clock time: the FIRST occurrence, AEDT, per schedule.to_utc", () => {
    assert.equal(localToInstant("2026-04-05", "02:30", "Australia/Sydney"), "2026-04-04T15:30:00.000Z");
  });

  test("Santiago 2026-09-06 00:30 is a skipped (spring-forward) wall-clock time: the offset from BEFORE the change, per schedule.to_utc", () => {
    assert.equal(localToInstant("2026-09-06", "00:30", "America/Santiago"), "2026-09-06T04:30:00.000Z");
  });
});

describe("requestBody", () => {
  const tz = "Europe/Amsterdam";

  test("whole days: sends {first_day, last_day}, never starts_at/ends_at", () => {
    const body = requestBody({ allDay: true, firstDay: "2026-10-27", lastDay: "2026-11-02", startTime: "09:00", endTime: "11:00", reason: "Holiday", tz });
    assert.deepEqual(body, { first_day: "2026-10-27", last_day: "2026-11-02", reason: "Holiday" });
  });

  test("partial: sends {starts_at, ends_at} built from date + time in the business zone, never first_day/last_day", () => {
    const body = requestBody({ allDay: false, firstDay: "2026-10-14", lastDay: "2026-10-14", startTime: "09:00", endTime: "11:00", reason: "Dentist", tz });
    assert.deepEqual(body, {
      starts_at: localToInstant("2026-10-14", "09:00", tz),
      ends_at: localToInstant("2026-10-14", "11:00", tz),
      reason: "Dentist",
    });
  });

  test("a blank reason is omitted on create, never sent as an empty string (the server refuses one)", () => {
    const body = requestBody({ allDay: true, firstDay: "2026-10-27", lastDay: "2026-10-27", startTime: "09:00", endTime: "11:00", reason: "   ", tz });
    assert.deepEqual(body, { first_day: "2026-10-27", last_day: "2026-10-27" });
    assert.equal("reason" in body, false);
  });
});

describe("patchBody", () => {
  const tz = "Europe/Amsterdam";
  const wholeDay = { allDay: true, firstDay: "2026-10-27", lastDay: "2026-11-02", startTime: "09:00", endTime: "17:00", reason: "Holiday", tz };
  const partial = { allDay: false, firstDay: "2026-10-14", lastDay: "2026-10-14", startTime: "09:00", endTime: "11:00", reason: "Dentist", tz };

  test("clearing the reason sends an explicit null, never omits the field (omitting would leave the old one in place)", () => {
    const patch = patchBody(partial, { ...partial, reason: "   " });
    assert.deepEqual(patch, { reason: null });
  });

  test("only the reason changed: the patch carries only reason, never resending unchanged times", () => {
    const patch = patchBody(partial, { ...partial, reason: "Doctor" });
    assert.deepEqual(patch, { reason: "Doctor" });
  });

  test("only the end time changed: the patch carries only ends_at", () => {
    const patch = patchBody(partial, { ...partial, endTime: "12:00" });
    assert.deepEqual(patch, { ends_at: localToInstant("2026-10-14", "12:00", tz) });
  });

  test("only the last day changed (whole days): the patch carries only last_day", () => {
    const patch = patchBody(wholeDay, { ...wholeDay, lastDay: "2026-11-03" });
    assert.deepEqual(patch, { last_day: "2026-11-03" });
  });

  test("nothing changed: an empty patch", () => {
    assert.deepEqual(patchBody(partial, { ...partial }), {});
  });

  test("switching kind sends the complete new pair, never a subset of the old one", () => {
    const patch = patchBody(partial, { ...partial, allDay: true, firstDay: "2026-10-20", lastDay: "2026-10-22" });
    assert.deepEqual(patch, { first_day: "2026-10-20", last_day: "2026-10-22" });
  });
});

describe("listWindow", () => {
  const tz = "Europe/Amsterdam";

  test("from is today's local midnight in the business zone, to is horizonDays later, never a hardcoded 365/366", () => {
    const now = new Date("2026-09-23T20:00:00Z"); // 22:00 in Amsterdam already, still 23rd there
    const window = listWindow(now, tz, 10);
    assert.equal(window.from, localToInstant("2026-09-23", "00:00", tz));
    assert.equal(window.to, localToInstant("2026-10-03", "00:00", tz));
  });

  test("uses the business zone's date, not the runner's own TZ (Sao Paulo, 3h behind)", () => {
    // 2026-09-23T02:00Z is already 2026-09-23 04:00 in Amsterdam, but still 2026-09-22 23:00 in
    // Sao Paulo: a window keyed off the runner's zone would start a day early.
    const now = new Date("2026-09-23T02:00:00Z");
    const window = listWindow(now, tz, 5);
    assert.equal(window.from, localToInstant("2026-09-23", "00:00", tz));
  });

  test("today is the business zone's date even when UTC's own date is already the next day (fences a `toISOString().slice(0,10)` reading of 'now')", () => {
    // 2026-09-22T23:30Z is 2026-09-22 in UTC, but already 2026-09-23 01:30 in Amsterdam (CEST,
    // +2h): a `now.toISOString().slice(0, 10)` implementation reads "2026-09-22" here, one day
    // short of the business zone's actual today.
    const now = new Date("2026-09-22T23:30:00Z");
    const window = listWindow(now, tz, 3);
    assert.equal(window.from, localToInstant("2026-09-23", "00:00", tz));
  });

  test("to is a calendar 10 days later even across the October fall-back (fences a `from + n * 86_400_000` reading of 'to')", () => {
    // 2026-10-25 has 25 real hours in Amsterdam (the clocks fall back). A `to` computed as a
    // fixed n*24h offset from `from`'s instant lands one calendar day short (2026-10-29) once
    // that extra hour is inside the window; calendar-day arithmetic lands on the correct 2026-10-30.
    const now = new Date("2026-10-20T08:00:00Z");
    const window = listWindow(now, tz, 10);
    assert.equal(window.to, localToInstant("2026-10-30", "00:00", tz));
  });
});

describe("blockLabel", () => {
  const tz = "Europe/Amsterdam";

  test("a single whole day: kind day, never a time (no '00:00 to 00:00' from the null instants)", () => {
    const label = blockLabel({ starts_at: null, ends_at: null, first_day: "2026-10-03", last_day: "2026-10-03" }, tz);
    assert.deepEqual(label, { kind: "day", date: "2026-10-03" });
  });

  test("a run of whole days: kind range, with the inclusive day count from the API's own fields", () => {
    const label = blockLabel({ starts_at: null, ends_at: null, first_day: "2026-10-27", last_day: "2026-11-02" }, tz);
    assert.deepEqual(label, { kind: "range", from: "2026-10-27", to: "2026-11-02", days: 7 });
  });

  test("a partial block: kind partial, date and times read in the business zone, never UTC", () => {
    const label = blockLabel({ starts_at: "2026-10-14T07:00:00.000Z", ends_at: "2026-10-14T09:00:00.000Z", first_day: null, last_day: null }, tz);
    assert.deepEqual(label, { kind: "partial", date: "2026-10-14", start: "09:00", end: "11:00" });
  });

  test("a partial block just before midnight UTC, already the next day in the business zone (fences the date read as the UTC date)", () => {
    // 2026-10-14T23:30Z is 2026-10-14 in UTC, but 2026-10-15 01:30 in Amsterdam (CEST, +2h):
    // reading the date off the raw instant (UTC) would report the 14th, one day early.
    const label = blockLabel({ starts_at: "2026-10-14T23:30:00.000Z", ends_at: "2026-10-15T00:30:00.000Z", first_day: null, last_day: null }, tz);
    assert.deepEqual(label, { kind: "partial", date: "2026-10-15", start: "01:30", end: "02:30" });
  });

  test("a partial block spanning days (starts_at 07:00Z the 14th, ends_at 09:00Z the 16th): kind partialRange, both dates shown, never just the start day", () => {
    const label = blockLabel({ starts_at: "2026-10-14T07:00:00.000Z", ends_at: "2026-10-16T09:00:00.000Z", first_day: null, last_day: null }, tz);
    assert.deepEqual(label, { kind: "partialRange", from: "2026-10-14", to: "2026-10-16", start: "09:00", end: "11:00" });
  });
});

describe("beyondHorizon", () => {
  const tz = "Europe/Amsterdam";
  const windowTo = localToInstant("2026-10-03", "00:00", tz);

  test("a whole-day block starting on the window's last visible day is within it", () => {
    assert.equal(beyondHorizon({ starts_at: null, first_day: "2026-10-02" }, tz, windowTo), false);
  });

  test("a whole-day block starting on (or after) the window's exclusive end is beyond it", () => {
    assert.equal(beyondHorizon({ starts_at: null, first_day: "2026-10-03" }, tz, windowTo), true);
  });

  test("a partial block is compared by its own instant, not by rounding its date", () => {
    assert.equal(beyondHorizon({ starts_at: "2026-10-02T20:30:00.000Z", first_day: null }, tz, windowTo), false);
    assert.equal(beyondHorizon({ starts_at: "2026-10-02T23:30:00.000Z", first_day: null }, tz, windowTo), true);
  });
});

describe("canEditBlock", () => {
  test("an owner may change or remove anyone's manual block", () => {
    assert.equal(canEditBlock("owner", false, "manual"), true);
    assert.equal(canEditBlock("owner", true, "manual"), true);
  });

  test("a worker may only change or remove their own manual block, never a colleague's", () => {
    assert.equal(canEditBlock("worker", true, "manual"), true);
    assert.equal(canEditBlock("worker", false, "manual"), false);
  });

  test("a Google-sourced block is read only for everyone, owner included (PATCH/DELETE 404 on it)", () => {
    assert.equal(canEditBlock("owner", true, "google"), false);
    assert.equal(canEditBlock("owner", false, "google"), false);
    assert.equal(canEditBlock("worker", true, "google"), false);
  });
});

describe("clientProblem", () => {
  const tz = "Europe/Amsterdam";
  const wholeDay = { allDay: true, firstDay: "2026-10-27", lastDay: "2026-11-02", startTime: "", endTime: "", reason: "", tz };
  const partial = { allDay: false, firstDay: "2026-10-14", lastDay: "2026-10-14", startTime: "09:00", endTime: "11:00", reason: "", tz };

  test("a whole-day block needs both days before the order check runs", () => {
    assert.equal(clientProblem({ ...wholeDay, firstDay: "" }), "missingFirstDay");
    assert.equal(clientProblem({ ...wholeDay, lastDay: "" }), "missingLastDay");
    assert.equal(clientProblem(wholeDay), null);
  });

  test("a partial block needs the date, the start time and the end time before `localToInstant` ever runs (each checked on its own, not lumped into one 'missing' state)", () => {
    assert.equal(clientProblem({ ...partial, firstDay: "" }), "missingFirstDay");
    assert.equal(clientProblem({ ...partial, startTime: "" }), "missingStart");
    assert.equal(clientProblem({ ...partial, endTime: "" }), "missingEnd");
    assert.equal(clientProblem(partial), null);
  });

  test("order checks still run once every field is present", () => {
    assert.equal(clientProblem({ ...wholeDay, lastDay: "2026-10-01" }), "lastDayBeforeFirst");
    assert.equal(clientProblem({ ...partial, endTime: "09:00" }), "endNotAfterStart");
  });
});
