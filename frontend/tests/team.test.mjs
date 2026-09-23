process.env.TZ = "America/Sao_Paulo";

import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { canInvite, expiresIn } from "../lib/team.ts";

describe("canInvite", () => {
  test("owner may invite", () => {
    assert.equal(canInvite("owner"), true);
  });

  test("worker may not invite", () => {
    assert.equal(canInvite("worker"), false);
  });
});

describe("expiresIn", () => {
  test("4 days and 1 hour away rounds up to 5", () => {
    const now = new Date("2026-09-23T00:00:00Z");
    const expiresAt = "2026-09-27T01:00:00Z";
    assert.equal(expiresIn(expiresAt, now), 5);
  });

  test("an expired invite is 0, never negative", () => {
    const now = new Date("2026-09-23T00:00:00Z");
    const expiresAt = "2026-09-20T00:00:00Z";
    assert.equal(expiresIn(expiresAt, now), 0);
  });

  test("exactly on the boundary is 0, not 1", () => {
    const now = new Date("2026-09-23T00:00:00Z");
    assert.equal(expiresIn(now.toISOString(), now), 0);
  });
});
