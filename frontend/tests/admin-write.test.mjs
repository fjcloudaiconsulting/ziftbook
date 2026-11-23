// ZIF-73/78: the phases of an admin write that needs a fresh second factor.
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { initialRecheck, recheck } from "../lib/admin-write.ts";

const run = (state, ...events) => events.reduce((acc, event) => ({ ...recheck(acc.state, event) }), { state });

describe("the write's answer", () => {
  // FENCE recheck: a stale factor opens the code form and never signs the admin out.
  test("a stale or missing factor opens the code form", () => {
    for (const code of ["second_factor_stale", "second_factor_required"]) {
      const out = recheck(initialRecheck, { type: "write", status: 403, code });
      assert.equal(out.state.phase, "code");
      assert.equal(out.then, "recheck");
    }
  });

  test("done, sign-in, changed, not found, invalid and other leave the phase alone", () => {
    for (const [status, then] of [[204, "done"], [401, "signIn"], [403, "signIn"], [409, "changed"], [404, "notFound"], [422, "invalid"], [500, "other"]]) {
      const out = recheck(initialRecheck, { type: "write", status });
      assert.deepEqual(out, { state: initialRecheck, then });
    }
  });
});

describe("the code's answer", () => {
  const inCode = recheck(initialRecheck, { type: "write", status: 403, code: "second_factor_stale" }).state;

  // FENCE resend: a passed code goes back to the reason phase and asks for the write again, once.
  test("reason, stale, code, passed: send the write again", () => {
    const out = run(initialRecheck, { type: "write", status: 403, code: "second_factor_stale" }, { type: "code", status: 200 });
    assert.deepEqual(out, { state: initialRecheck, then: "resend" });
  });

  test("a wrong code stays on the code form", () => {
    assert.deepEqual(recheck(inCode, { type: "code", status: 401, code: "invalid_code" }), { state: inCode, then: "wrong" });
  });

  test("an ended session or a conflict goes to sign-in", () => {
    assert.equal(recheck(inCode, { type: "code", status: 401 }).then, "signIn");
    assert.equal(recheck(inCode, { type: "code", status: 409 }).then, "signIn");
  });

  test("a locked or unavailable app leaves only a recovery code, for good", () => {
    for (const code of ["second_factor_locked", "second_factor_unavailable"]) {
      const out = recheck(inCode, { type: "code", status: 403, code });
      assert.equal(out.then, "blocked");
      assert.equal(out.state.kind, "recovery");
      assert.equal(recheck(out.state, { type: "switch", kind: "app" }).state.kind, "recovery");
    }
  });

  test("too many tries and anything else", () => {
    assert.equal(recheck(inCode, { type: "code", status: 429 }).then, "tooMany");
    assert.equal(recheck(inCode, { type: "code", status: 500 }).then, "other");
  });
});

describe("switching and resetting", () => {
  test("the admin can switch between the app and a recovery code, and reset", () => {
    const recovery = recheck(initialRecheck, { type: "switch", kind: "recovery" }).state;
    assert.equal(recovery.kind, "recovery");
    assert.equal(recheck(recovery, { type: "switch", kind: "app" }).state.kind, "app");
    assert.deepEqual(recheck(recovery, { type: "reset" }).state, initialRecheck);
  });
});
