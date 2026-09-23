// Pure logic for blocked time (PR 6, ZIF-50 / ZIF-103): local-time-to-instant conversion (DST
// correct, no browser midnight math), the request body per kind, the list window (today ..
// today+horizon), list labels, and who may edit a block.
process.env.TZ = "America/Sao_Paulo";

import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { localToInstant } from "../lib/time-off.ts";

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
