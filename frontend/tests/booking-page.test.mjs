// Pure logic behind the public booking page (ZIF-56): cancellation terms, the business's own
// policy text, email typo suggestions, the POST body, the answer-to-outcome mapping, day-part
// grouping in the business's own time zone, and the availability window.
process.env.TZ = "America/Sao_Paulo"; // F6: the viewer's zone must never leak into business-local grouping

import assert from "node:assert/strict";
import { describe, test } from "node:test";

import {
  afterPickTaken,
  answerState,
  bookingBody,
  cancellationState,
  confirmScreen,
  dayPart,
  emailSuggestion,
  firstFreeDayFrom,
  freeNamed,
  groupByDayPart,
  holdBody,
  holdEnded,
  keepPick,
  nextWeekDisabled,
  ownPolicyText,
  scanWindow,
  signedInAs,
  slotsFor,
  slotWho,
  slugLooksValid,
  withName,
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

  test("fence: a legitimately different provider close in spelling to a known one is never flagged", () => {
    // Wrong implementation killed: leaving ymail.com/mail.com/email.com out of KNOWN_DOMAINS, so
    // each gets "corrected" toward gmail.com/mail.com/gmail.com respectively even though every one
    // of them is a real provider in its own right.
    assert.equal(emailSuggestion("ana@ymail.com"), null);
    assert.equal(emailSuggestion("ana@mail.com"), null);
    assert.equal(emailSuggestion("ana@email.com"), null);
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
    // 04:00 Amsterdam (CEST, UTC+2) is morning; the SAME instant is 23:00 the PREVIOUS day in
    // Sao_Paulo (UTC-3) — evening. A viewer-zone leak would answer "evening" here, unlike the
    // other two cases below, which happen to land in a plausible-but-wrong part either way.
    assert.equal(dayPart("2026-10-20T02:00:00Z", ZONE), "morning");
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

describe("slugLooksValid: mirrors backend_page.py's own slug check, so a bad slug never reaches it", () => {
  test("fence: uppercase is accepted (lowered first, like the backend), but non-ascii and long slugs are not", () => {
    // Wrong implementation killed: rejecting uppercase outright (the backend lowers before
    // checking, so "Carol-Nails" is a valid, findable slug, not a 404 by itself).
    assert.equal(slugLooksValid("Carol-Nails"), true);
    assert.equal(slugLooksValid("carol-nails-bar"), true);
    assert.equal(slugLooksValid("a".repeat(41)), false);
    assert.equal(slugLooksValid(""), false);
    assert.equal(slugLooksValid("-leading"), false);
    assert.equal(slugLooksValid("has space"), false);
  });

  test("fence: the ASCII check runs on the RAW slug, not the lowercased one", () => {
    // "Karol" spelled with U+212A KELVIN SIGN in place of the ordinary "K": String.toLowerCase()
    // folds it to plain ASCII "k", so "karol" alone is indistinguishable from a real ASCII slug.
    // Wrong implementation killed: checking isascii on `slug.toLowerCase()` instead of `slug` itself,
    // which would let this through (café's "é" survives lowercasing and would be caught either way,
    // so it never exercised this ordering).
    const kelvinK = "Karol";
    assert.equal(kelvinK.toLowerCase(), "karol"); // guard: confirms the fold this fence depends on
    assert.equal(slugLooksValid(kelvinK), false);
  });
});

describe("B8 firstFreeDayFrom: the already-fetched window is checked before any further scan", () => {
  const ZONE = "Europe/Amsterdam";

  test("fence: a free day in the SAME 14-day window's second week is found without a new fetch", () => {
    // Wrong implementation killed: only looking at the visible 7 days (or requiring the whole
    // 14-day window to be empty before checking anything), which misses a day the scanWindow
    // fetch already returned in the *following* week and would trigger a needless extra request.
    const weekStart = "2026-10-01"; // the visible week: 2026-10-01..2026-10-07, empty
    const slots = ["2026-10-11T09:00:00Z"]; // in the SAME fetched window (from+13 = 10-14), week 2
    assert.equal(firstFreeDayFrom(slots, weekStart, ZONE), "2026-10-11");
  });

  test("fence: a day before `from` is never returned, even if it sorts first", () => {
    // Wrong implementation killed: ignoring the `from` lower bound, which could point the client
    // back at a day before the week they're currently looking at.
    const slots = ["2026-09-28T09:00:00Z", "2026-10-05T09:00:00Z"];
    assert.equal(firstFreeDayFrom(slots, "2026-10-01", ZONE), "2026-10-05");
  });

  test("no free day anywhere in the window is null", () => {
    assert.equal(firstFreeDayFrom([], "2026-10-01", ZONE), null);
  });
});

describe("ZIF-100 who is free at a time", () => {
  // The availability response's workers are sorted by id; the page's roster is sorted by name.
  const workers = [{ id: "a1" }, { id: "b2" }, { id: "c3" }];
  const roster = [
    { id: "c3", display_name: "Ana" },
    { id: "a1", display_name: null },
    { id: "b2", display_name: "Bea" },
  ];

  test("fence: slot_workers indices resolve through the response's own workers, not the roster", () => {
    // Wrong implementation killed: returning the raw indices, or an off-by-one into `workers`.
    assert.deepEqual(slotWho([[0, 2], [1]], workers), [["a1", "c3"], ["b2"]]);
  });

  test("fence: a person's week is the shared window filtered to their times, and the empty-week scan sees only those", () => {
    // Wrong implementation killed: ignoring the person (the whole team's times shown under one
    // name), which also makes the scan stop at a day only someone else is free.
    const win = { slots: ["2026-10-02T09:00:00Z", "2026-10-09T09:00:00Z"], who: [["a1"], ["a1", "b2"]] };
    assert.deepEqual(slotsFor(win, "any"), win.slots);
    assert.deepEqual(slotsFor(win, "b2"), ["2026-10-09T09:00:00Z"]);
    assert.equal(firstFreeDayFrom(slotsFor(win, "b2"), "2026-10-01", "Europe/Amsterdam"), "2026-10-09");
  });

  test("fence: free named people come in roster order, without unnamed people or unknown ids", () => {
    // Wrong implementation killed: keeping availability (id) order, offering an unnamed person, or
    // offering an id the page roster does not know.
    assert.deepEqual(freeNamed(roster, ["a1", "b2", "c3", "zz"]).map((w) => w.id), ["c3", "b2"]);
    assert.deepEqual(freeNamed(roster, ["a1"]), []);
  });

  test("fence: a pick is kept only when that person is free at the new time and there is a choice", () => {
    // Wrong implementation killed: always keeping (books someone who is busy then), always
    // clearing, or keeping a pick where only that one person is free (that books as no preference).
    // An unnamed person free too (a1) is not a choice: Bea alone is still booked as no preference.
    assert.equal(keepPick("b2", roster, ["b2", "c3"]), "b2");
    assert.equal(keepPick("b2", roster, ["a1", "c3"]), null);
    assert.equal(keepPick("b2", roster, ["b2"]), null);
    assert.equal(keepPick("b2", roster, ["b2", "a1"]), null);
    assert.equal(keepPick(null, roster, ["b2", "c3"]), null);
  });

  test("fence: the With line names the step 3 pick first, then the filter; an unnamed person gives no line", () => {
    // Wrong implementation killed: ignoring the pick ("Anyone available" after choosing Bea), or
    // rendering an unnamed person as an empty name.
    assert.equal(withName(roster, "any", null, "Anyone"), "Anyone");
    assert.equal(withName(roster, "any", "b2", "Anyone"), "Bea");
    assert.equal(withName(roster, "c3", null, "Anyone"), "Ana");
    assert.equal(withName(roster, "a1", null, "Anyone"), null);
  });

  test("fence: after the picked person is taken, stay only when someone else is still free then", () => {
    // Wrong implementation killed: staying whenever the slot is still offered (even if only the
    // taken person is listed), or keeping the taken person in the list.
    assert.deepEqual(afterPickTaken(["a1", "b2", "c3"], "b2"), ["a1", "c3"]);
    assert.equal(afterPickTaken(["b2"], "b2"), null);
    assert.equal(afterPickTaken(null, "b2"), null);
  });
});

describe("ZIF-117 confirm by email", () => {
  test("fence: only the signed-in address itself books straight away", () => {
    assert.equal(signedInAs(null, "ana@example.com"), false);
    assert.equal(signedInAs("ana@example.com", " Ana@Example.com "), true);
    assert.equal(signedInAs("ana@example.com", "ana@example.org"), false);
    assert.equal(signedInAs("ana@example.com", ""), false);
  });

  test("fence: the hold body carries the address and the time only, replaces only when set", () => {
    const form = { startsAt: "2026-10-15T08:30:00+00:00", memberId: null, email: " ana@example.com ", locale: "pt", turnstileToken: "tok", replaces: null };
    assert.deepEqual(holdBody(form), { starts_at: "2026-10-15T08:30:00+00:00", member_id: null, email: "ana@example.com", locale: "pt", turnstile_token: "tok" });
    assert.equal(holdBody({ ...form, replaces: "s3cret" }).replaces, "s3cret");
  });

  test("fence: 202 is the hold's done and 403 verify_email is its own outcome, never 'verify failed'", () => {
    assert.equal(answerState({ status: 202 }).kind, "done");
    assert.equal(answerState({ status: 403, code: "verify_email" }).kind, "verifyEmail");
    assert.equal(answerState({ status: 403, code: "turnstile_failed" }).kind, "verifyFailed");
  });

  test("fence: every 404 on the click is the dead link, every 409 the taken time", () => {
    assert.equal(confirmScreen({ status: 201 }), "done");
    for (const code of ["link_expired", "not_found"]) assert.equal(confirmScreen({ status: 404, code }), "dead");
    for (const code of ["slot_unavailable", "slot_taken"]) assert.equal(confirmScreen({ status: 409, code }), "taken");
    assert.equal(confirmScreen({ status: 429, code: "rate_limited" }), "tooMany");
    assert.equal(confirmScreen({ status: 422, code: "unknown_policy_version" }), "policyChanged");
    assert.equal(confirmScreen({ status: 422, code: "invalid_request" }), "fieldErrors");
    for (const status of [0, 500, 503]) assert.equal(confirmScreen({ status }), "unknown");
  });

  test("fence: the hold has ended AT its until, not a minute later", () => {
    const until = "2026-10-15T08:47:00Z";
    assert.equal(holdEnded(until, new Date("2026-10-15T08:46:59Z")), false);
    assert.equal(holdEnded(until, new Date("2026-10-15T08:47:00Z")), true);
  });
});
