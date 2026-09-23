// Pure logic for blocked time (PR 6, ZIF-50 / ZIF-103): local-time-to-instant conversion (DST
// correct, no browser midnight math), the request body per kind, the list window (today ..
// today+horizon), list labels, and who may edit a block.
process.env.TZ = "America/Sao_Paulo";

import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { listWindow, localToInstant, requestBody } from "../lib/time-off.ts";

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
