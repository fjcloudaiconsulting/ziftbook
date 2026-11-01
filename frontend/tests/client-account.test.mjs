// ZIF-146: the logic behind a client's own account: signing in to it, filling the checkout from it, its bookings
// and the way back to a booking page after an emailed sign-up.
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { accountSignInOutcome } from "../lib/client-account.ts";

describe("account sign-in", () => {
  test("a wrong password is the console's own generic answer, never a hint that the email has an account", () => {
    assert.equal(accountSignInOutcome(401, "invalid_credentials", true), "invalid");
  });

  test("a platform admin's identity gets no account session: its own message, only after a correct password", () => {
    assert.equal(accountSignInOutcome(403, "account_unavailable", true), "unavailable");
  });

  test("the challenge and the rate limit keep the console sign-in's meaning", () => {
    assert.equal(accountSignInOutcome(429, "challenge_required", true), "challenge");
    assert.equal(accountSignInOutcome(429, "challenge_required", false), "try_later");
    assert.equal(accountSignInOutcome(429, "rate_limited", true), "try_later");
  });

  test("the rest", () => {
    assert.equal(accountSignInOutcome(200, undefined, true), "ok");
    assert.equal(accountSignInOutcome(403, "turnstile_failed", true), "error");
    assert.equal(accountSignInOutcome(503, "busy", true), "error");
  });
});
