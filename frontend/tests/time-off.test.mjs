// Pure logic for blocked time (PR 6, ZIF-50 / ZIF-103): local-time-to-instant conversion (DST
// correct, no browser midnight math), the request body per kind, the list window (today ..
// today+horizon), list labels, and who may edit a block.
process.env.TZ = "America/Sao_Paulo";

import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { localToInstant, requestBody } from "../lib/time-off.ts";

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
