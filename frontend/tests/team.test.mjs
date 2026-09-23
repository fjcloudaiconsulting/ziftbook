process.env.TZ = "America/Sao_Paulo";

import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { canInvite, expiresIn, isRowExpired, upsertInvite } from "../lib/team.ts";

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

describe("isRowExpired", () => {
  test("fence: zero days left reads as expired even when the server's flag hasn't caught up (client clock ahead)", () => {
    const now = new Date("2026-09-23T00:00:00Z");
    assert.equal(isRowExpired({ email: "a@x.com", expires_at: now.toISOString(), expired: false }, now), true);
  });

  test("guard: the server's own expired flag alone is enough", () => {
    const now = new Date("2026-09-23T00:00:00Z");
    assert.equal(isRowExpired({ email: "a@x.com", expires_at: "2099-01-01T00:00:00Z", expired: true }, now), true);
  });

  test("guard: days left and not expired is not expired", () => {
    const now = new Date("2026-09-23T00:00:00Z");
    assert.equal(isRowExpired({ email: "a@x.com", expires_at: "2026-09-30T00:00:00Z", expired: false }, now), false);
  });
});

describe("upsertInvite", () => {
  const invite = (email, id, expires_at = "2026-10-01T00:00:00Z") => ({ id, email, expires_at, expired: false });

  test("fence: re-inviting the same email (case-insensitive) replaces the row in place, never a second row", () => {
    const list = [invite("a@x.com", "1"), invite("b@x.com", "2")];
    const resent = invite("A@X.com", "3", "2026-10-08T00:00:00Z");
    const result = upsertInvite(list, resent);
    assert.equal(result.length, 2);
    assert.equal(result[0].id, "3");
    assert.equal(result[1].id, "2");
  });

  test("fence: a new email is appended, never dropped", () => {
    const list = [invite("a@x.com", "1")];
    const result = upsertInvite(list, invite("c@x.com", "9"));
    assert.deepEqual(
      result.map((r) => r.id),
      ["1", "9"],
    );
  });

  test("fence: kills filter-then-append (dedupes correctly but moves the resent row to the end)", () => {
    const list = [invite("a@x.com", "1"), invite("b@x.com", "2"), invite("c@x.com", "3")];
    const resent = invite("b@x.com", "9");
    const result = upsertInvite(list, resent);
    assert.deepEqual(
      result.map((r) => r.id),
      ["1", "9", "3"],
    );
  });
});
