// Pure logic for blocked time (PR 6, ZIF-50 / ZIF-103): local-time-to-instant conversion (DST
// correct, no browser midnight math), the request body per kind, the list window (today ..
// today+horizon), list labels, and who may edit a block.
process.env.TZ = "America/Sao_Paulo";

import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { blockLabel, canEditBlock, listWindow, localToInstant, requestBody } from "../lib/time-off.ts";

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

  test("a blank reason is omitted, never sent as an empty string (the server refuses one)", () => {
    const body = requestBody({ allDay: true, firstDay: "2026-10-27", lastDay: "2026-10-27", startTime: "09:00", endTime: "11:00", reason: "   ", tz });
    assert.deepEqual(body, { first_day: "2026-10-27", last_day: "2026-10-27" });
    assert.equal("reason" in body, false);
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
