// Pure logic behind the public booking page (ZIF-56 PR2): cancellation terms, the business's own
// policy text, email typo suggestions, the POST body, the answer-to-outcome mapping, day-part
// grouping in the business's own time zone, and the availability window.
process.env.TZ = "America/Sao_Paulo"; // F6: the viewer's zone must never leak into business-local grouping

import assert from "node:assert/strict";
import { describe, test } from "node:test";

import {
  answerState,
  bookingBody,
  cancellationState,
  dayPart,
  emailSuggestion,
  groupByDayPart,
  nextWeekDisabled,
  ownPolicyText,
  scanWindow,
} from "../lib/booking-page.ts";

describe("F1 cancellation terms", () => {
  test("free-until is the slot start minus the free-cancellation hours, not now plus hours", () => {
    // Wrong implementation killed: now + hours, which drifts with how late the client loads the
    // page and ignores the chosen slot entirely.
    const slotStart = new Date("2026-10-20T15:00:00Z");
    const now = new Date("2026-10-01T00:00:00Z");
    const state = cancellationState(now, slotStart, 24);
    assert.equal(state.kind, "before");
    assert.equal(state.freeUntil.toISOString(), "2026-10-19T15:00:00.000Z");
  });

  test("fence: the exact boundary instant is free on one side, inside the window on the other", () => {
    const slotStart = new Date("2026-10-20T15:00:00Z");
    const freeUntil = new Date("2026-10-19T15:00:00.000Z");
    assert.equal(cancellationState(freeUntil, slotStart, 24).kind, "before");
    assert.equal(cancellationState(new Date(freeUntil.getTime() + 1), slotStart, 24).kind, "inside");
  });
});

describe("F2 the business's own policy text", () => {
  test("shown only when the page locale matches the business language", () => {
    assert.equal(ownPolicyText("Some terms", "nl", "nl"), "Some terms");
    assert.equal(ownPolicyText("Some terms", "en", "nl"), null);
  });

  test("fence: null or empty text is never quoted, even when the locale matches", () => {
    // Wrong implementation killed: showing the quote block whenever locale matches, regardless of
    // whether the business ever wrote any text.
    assert.equal(ownPolicyText(null, "nl", "nl"), null);
    assert.equal(ownPolicyText("", "nl", "nl"), null);
  });
});

describe("F3 email typo suggestion", () => {
  test("fence: an exact known domain never suggests itself", () => {
    // Wrong implementation killed: comparing distance only, which treats gmail.com as its own
    // 0-edit-distance-but-still-shown typo.
    assert.equal(emailSuggestion("ana@gmail.com"), null);
  });

  test("a close misspelling suggests the known domain, local part ignored", () => {
    assert.equal(emailSuggestion("ana@gmial.com"), "ana@gmail.com");
    assert.equal(emailSuggestion("literally.anything+tag@gmial.com"), "literally.anything+tag@gmail.com");
  });

  test("fence: edit distance 3 or more suggests nothing", () => {
    assert.equal(emailSuggestion("ana@gmailxyz.com"), null); // distance 3 from gmail.com
    assert.equal(emailSuggestion("ana@example.com"), null);
  });
});

describe("F4 booking POST body", () => {
  const base = {
    startsAt: "2026-10-20T15:00:00Z",
    memberId: null,
    name: "Ana",
    email: "ana@example.com",
    phone: "",
    locale: "nl",
    policyVersion: "2026-01-01",
    turnstileToken: "tok",
  };

  test("fence: a blank phone is omitted, never sent as an empty string", () => {
    // Wrong implementation killed: `phone: ""`, which 422s on every phone-less booking
    // (main.py's strict body).
    const body = bookingBody(base);
    assert.equal("phone" in body, false);
  });

  test("a typed phone, trimmed, is sent", () => {
    assert.equal(bookingBody({ ...base, phone: "  +31 6 1234  " }).phone, "+31 6 1234");
  });

  test("fence: starts_at is passed through byte for byte, never re-serialised", () => {
    // Wrong implementation killed: `new Date(startsAt).toISOString()`, which turns a "+00:00"
    // offer into "...Z" (or vice versa) and no longer matches what the availability GET offered.
    const body = bookingBody({ ...base, startsAt: "2026-10-20T15:00:00+00:00" });
    assert.equal(body.starts_at, "2026-10-20T15:00:00+00:00");
  });

  test("consents is always {}, locale is present, member_id null means anyone", () => {
    const body = bookingBody(base);
    assert.deepEqual(body.consents, {});
    assert.equal(body.locale, "nl");
    assert.equal(body.member_id, null);
  });

  test("fence: the key set is exactly the allowed list, nothing else, phone included when present", () => {
    assert.deepEqual(
      Object.keys(bookingBody(base)).sort(),
      ["consents", "email", "locale", "member_id", "name", "policy_version", "starts_at", "turnstile_token"].sort(),
    );
    assert.deepEqual(
      Object.keys(bookingBody({ ...base, phone: "123" })).sort(),
      ["consents", "email", "locale", "member_id", "name", "phone", "policy_version", "starts_at", "turnstile_token"].sort(),
    );
  });
});

describe("F5 answer -> outcome mapping", () => {
  test("fence: both slot-taken codes go back to the picker, not a generic retry", () => {
    assert.equal(answerState({ status: 409, code: "slot_taken" }).kind, "slotTaken");
    assert.equal(answerState({ status: 409, code: "slot_unavailable" }).kind, "slotTaken");
  });

  test("fence: 403 turnstile_failed is 'verify', never the network/unknown copy", () => {
    assert.equal(answerState({ status: 403, code: "turnstile_failed" }).kind, "verifyFailed");
  });

  test("fence: 0, 502 and 500 are the unknown outcome, never 'nothing was booked'", () => {
    for (const status of [0, 502, 500, 499]) {
      assert.equal(answerState({ status }).kind, "unknown");
    }
  });

  test("fence: only 503 busy is 'nothing booked'; any other 503 is unknown", () => {
    assert.equal(answerState({ status: 503, code: "busy" }).kind, "nothingBooked");
    assert.equal(answerState({ status: 503, code: "other" }).kind, "unknown");
    assert.equal(answerState({ status: 503 }).kind, "unknown");
  });

  test("fence: the policy-changed 422 codes refresh; any other 422 asks to check details", () => {
    assert.equal(answerState({ status: 422, code: "unknown_policy_version" }).kind, "policyChanged");
    assert.equal(answerState({ status: 422, code: "purpose_not_in_policy_version" }).kind, "policyChanged");
    assert.equal(answerState({ status: 422, code: "invalid_request" }).kind, "fieldErrors");
  });

  test("404 means the service is gone; 429 is the neutral too-many-attempts; 201 is done", () => {
    assert.equal(answerState({ status: 404 }).kind, "serviceGone");
    assert.equal(answerState({ status: 429 }).kind, "tooMany");
    assert.equal(answerState({ status: 201 }).kind, "done");
  });
});

describe("F6 day-part grouping and horizon, in the BUSINESS time zone", () => {
  // Guard: the runner's own TZ is America/Sao_Paulo, a business in Europe/Amsterdam.
  const ZONE = "Europe/Amsterdam";

  test("fence: grouping uses the business zone, not the viewer's (TZ=America/Sao_Paulo)", () => {
    // Wrong implementation killed: reading new Date(slot).getHours() (the viewer's own zone),
    // which would misclassify every one of these under America/Sao_Paulo (UTC-3 in October).
    assert.equal(dayPart("2026-10-20T09:30:00Z", ZONE), "morning"); // 11:30 Amsterdam (CEST, UTC+2)
    assert.equal(dayPart("2026-10-20T14:30:00Z", ZONE), "afternoon"); // 16:30 Amsterdam
    assert.equal(dayPart("2026-10-20T18:30:00Z", ZONE), "evening"); // 20:30 Amsterdam
  });

  test("grouping keeps each day's slots in order, split by part", () => {
    const slots = ["2026-10-20T09:30:00Z", "2026-10-20T14:30:00Z", "2026-10-20T06:00:00Z"];
    const grouped = groupByDayPart(slots, ZONE);
    assert.deepEqual(grouped.morning, ["2026-10-20T06:00:00Z", "2026-10-20T09:30:00Z"]);
    assert.deepEqual(grouped.afternoon, ["2026-10-20T14:30:00Z"]);
    assert.deepEqual(grouped.evening, []);
  });

  test("fence: the horizon limit is off by one in neither direction", () => {
    // Wrong implementation killed: `>` becomes `>=` (or vice versa), disabling one day too early
    // or allowing one day too late.
    assert.equal(nextWeekDisabled("2026-10-01", "2026-10-15", 14), false); // exactly the horizon: allowed
    assert.equal(nextWeekDisabled("2026-10-01", "2026-10-16", 14), true); // one day past: disabled
  });
});

describe("F10 availability window", () => {
  test("fence: the scan requests to = from + 13 days, never + 14 (kills the off-by-one 422)", () => {
    // availability.py:424 refuses (to - from).days >= 14, so from..from+13 is the largest legal
    // 14-day-inclusive window.
    assert.deepEqual(scanWindow("2026-10-01"), { from: "2026-10-01", to: "2026-10-14" });
  });
});
