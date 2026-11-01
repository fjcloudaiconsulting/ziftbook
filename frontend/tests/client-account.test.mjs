// ZIF-146: the logic behind a client's own account: signing in to it, filling the checkout from it, its bookings
// and the way back to a booking page after an emailed sign-up.
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { accountSignInOutcome, bookAgainSlug, historyOutcome, historySections, historyServiceName, statusPill } from "../lib/client-account.ts";

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

const booking = (startsAt, status = "confirmed", slug = "salon-bloom") => ({
  business: { name: "Salon Bloom", slug },
  starts_at: startsAt,
  ends_at: startsAt,
  status,
  service_name: { en: "Haircut" },
  price: { amount_minor: 3500, currency: "EUR" },
  worker_display_name: null,
});

describe("your bookings", () => {
  const now = new Date("2026-10-30T12:00:00Z");

  test("an answer: the list, sign in again, the password again for a session older than a day, or a problem", () => {
    assert.equal(historyOutcome(200, undefined), "ok");
    assert.equal(historyOutcome(401, "unauthenticated"), "signIn");
    assert.equal(historyOutcome(403, "reauthentication_required"), "reauth");
    assert.equal(historyOutcome(429, "rate_limited"), "tooMany");
    assert.equal(historyOutcome(403, "cross_site"), "error");
    assert.equal(historyOutcome(0, undefined), "error");
  });

  test("upcoming first, the next one at the top; then the past, newest first", () => {
    // The API pages newest first; the screen answers "what is next" first.
    const page = [
      booking("2026-11-04T17:30:00Z", "pending"),
      booking("2026-10-31T09:00:00Z"),
      booking("2026-09-27T09:00:00Z", "completed"),
      booking("2026-09-01T09:00:00Z", "cancelled_by_client"),
    ];
    const { upcoming, past } = historySections(page, now);
    assert.deepEqual(upcoming.map((b) => b.starts_at), ["2026-10-31T09:00:00Z", "2026-11-04T17:30:00Z"]);
    assert.deepEqual(past.map((b) => b.starts_at), ["2026-09-27T09:00:00Z", "2026-09-01T09:00:00Z"]);
  });

  test("a booking that has started is past", () => {
    const { upcoming, past } = historySections([booking("2026-10-30T12:00:00Z")], now);
    assert.deepEqual([upcoming.length, past.length], [0, 1]);
  });

  test("the status pill: waiting and confirmed ahead; nothing on a kept past booking; what went otherwise, muted", () => {
    assert.deepEqual(statusPill("pending", true), { label: "waiting", tone: "warn" });
    assert.deepEqual(statusPill("confirmed", true), { label: "confirmed", tone: "ok" });
    assert.equal(statusPill("confirmed", false), null);
    assert.equal(statusPill("completed", false), null);
    assert.deepEqual(statusPill("cancelled_by_client", false), { label: "cancelled", tone: "muted" });
    assert.deepEqual(statusPill("cancelled_by_merchant", true), { label: "cancelled", tone: "muted" });
    assert.deepEqual(statusPill("declined", false), { label: "declined", tone: "muted" });
    assert.deepEqual(statusPill("expired", false), { label: "expired", tone: "muted" });
    assert.deepEqual(statusPill("no_show", false), { label: "noShow", tone: "muted" });
    assert.deepEqual(statusPill("pending", false), { label: "waiting", tone: "warn" });
  });

  test("Book again: on past rows only, to the business's own page, and only for a slug that is one", () => {
    // Rows open nothing else: the history carries no booking id and no manage link (security N9).
    assert.equal(bookAgainSlug(booking("2026-09-27T09:00:00Z", "completed"), false), "salon-bloom");
    assert.equal(bookAgainSlug(booking("2026-11-04T09:00:00Z"), true), null);
    for (const slug of ["/evil.example", "javascript:alert(1)", "a/b", "", "Salon Bloom"]) {
      assert.equal(bookAgainSlug(booking("2026-09-27T09:00:00Z", "completed", slug), false), null, slug);
    }
  });

  test("a service's name in the reader's language, else the one there is", () => {
    assert.deepEqual(historyServiceName({ en: "Haircut", nl: "Knippen" }, "nl"), { text: "Knippen", lang: null });
    assert.deepEqual(historyServiceName({ pt: "Corte" }, "en"), { text: "Corte", lang: "pt" });
    assert.deepEqual(historyServiceName({}, "en"), { text: "", lang: null });
  });
});
