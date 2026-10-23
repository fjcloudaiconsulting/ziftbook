// Pure logic behind the public booking page (ZIF-56): cancellation terms, the business's own
// policy text, email typo suggestions, the POST body, the answer-to-outcome mapping, day-part
// grouping in the business's own time zone, and the availability window.
process.env.TZ = "America/Sao_Paulo"; // F6: the viewer's zone must never leak into business-local grouping

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

import {
  afterPickTaken,
  answerState,
  bookingBody,
  cancellationState,
  checkoutUrl,
  cleanMessage,
  dayPart,
  doneState,
  elementsOptions,
  emailSuggestion,
  entryMode,
  firstFreeDayFrom,
  freeNamed,
  groupByDayPart,
  keepPick,
  mayForget,
  nextWeekDisabled,
  ownPolicyText,
  parseCheckout,
  phoneValue,
  pickFromUrl,
  returnParams,
  scanWindow,
  sendAction,
  sendLabel,
  slotsFor,
  slotWho,
  slugLooksValid,
  STRIPE_LOCALE,
  storedNext,
  termsLines,
  weekFor,
  withName,
} from "../lib/booking-page.ts";
import { defaultDial, dialOptions, flagOf, matchDial } from "../lib/dial-codes.ts";

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
    phone: "  06 1234 5678  ",
    locale: "nl",
    policyVersion: "2026-01-01",
    turnstileToken: "tok",
    consent: false,
    message: null,
    payment: null,
  };

  test("ZIF-152 fence: the phone is always sent, trimmed (the server reads its format)", () => {
    // Wrong implementation killed: the old omit-when-blank, or a client-side reformat.
    assert.equal(bookingBody(base).phone, "06 1234 5678");
    assert.equal(bookingBody({ ...base, phone: " " }).phone, "");
  });

  test("fence: starts_at is passed through byte for byte, never re-serialised", () => {
    // Wrong implementation killed: `new Date(startsAt).toISOString()`, which turns a "+00:00"
    // offer into "...Z" (or vice versa) and no longer matches what the availability GET offered.
    const body = bookingBody({ ...base, startsAt: "2026-10-20T15:00:00+00:00" });
    assert.equal(body.starts_at, "2026-10-20T15:00:00+00:00");
  });

  test("fence: unticked sends consents {}, ticked {marketing_email: true}, never a false", () => {
    // Wrong implementation killed: posting {marketing_email: false}, which withdraws a signed-in
    // booker's earlier consent.
    assert.deepEqual(bookingBody(base).consents, {});
    assert.deepEqual(bookingBody({ ...base, consent: true }).consents, { marketing_email: true });
    assert.equal(bookingBody(base).locale, "nl");
    assert.equal(bookingBody(base).member_id, null);
  });

  test("guard: a blank message is sent as null", () => {
    assert.equal(bookingBody({ ...base, message: cleanMessage("  \n ") }).message, null);
    assert.equal(bookingBody({ ...base, message: cleanMessage(" hi ") }).message, "hi");
  });

  test("fence: the key set is exactly the allowed list, nothing else", () => {
    assert.deepEqual(
      Object.keys(bookingBody(base)).sort(),
      ["consents", "email", "locale", "member_id", "message", "name", "phone", "payment", "policy_version", "starts_at", "turnstile_token"].sort(),
    );
  });
});

describe("F5 answer -> outcome mapping", () => {
  test("fence: both slot-taken codes go back to the picker, not a generic retry", () => {
    assert.equal(answerState({ status: 409, code: "slot_taken" }).kind, "slotTaken");
    assert.equal(answerState({ status: 409, code: "slot_unavailable" }).kind, "slotTaken");
  });

  test("fence: 403 turnstile_failed is 'verify', never the network/unknown copy", () => {
    assert.equal(answerState({ status: 403, code: "turnstile_failed" }, true).kind, "verifyFailed");
  });

  test("fence (ZIF-116): with no widget, 403 turnstile_failed is 'unavailable', never 'try again'", () => {
    assert.equal(answerState({ status: 403, code: "turnstile_failed" }, false).kind, "unavailable");
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

  test("ZIF-152 fence: an unreadable phone is its own outcome, for the field", () => {
    assert.equal(answerState({ status: 422, code: "phone_invalid" }).kind, "phoneInvalid");
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

describe("message_invalid", () => {
  test("fence: 422 message_invalid is its own outcome, never the generic field errors banner", () => {
    // Wrong implementation killed: the generic fieldErrors banner (the message field is never marked).
    assert.equal(answerState({ status: 422, code: "message_invalid" }).kind, "messageInvalid");
    assert.equal(answerState({ status: 422, code: "invalid_request" }).kind, "fieldErrors");
  });
});

describe("ZIF-157 direct booking checkout", () => {
  const svc = { id: "svc-1", workers: [{ id: "w1", display_name: "Ana" }, { id: "w2", display_name: null }] };

  test("fence: the checkout URL carries service, with and at, and never the booker's details", () => {
    // Wrong implementations killed: PII in the URL (logs, Referer, history); losing with/at on
    // refresh; posting a stale member_id; a throw (or an Invalid Date pick) on a garbage `at`.
    const state = { serviceId: "svc-1", memberId: "w1", at: "2026-10-22T08:30:00+00:00", name: "Ana Silva", email: "ana@example.com", phone: "+31 6 1234 5678", message: "hello there" };
    const url = checkoutUrl("salon", state);
    for (const secret of ["Ana", "Silva", "example.com", "1234", "hello"]) assert.ok(!url.includes(secret), secret);
    const parsed = parseCheckout(new URL(url, "https://x.test").searchParams, svc);
    assert.deepEqual(parsed, { serviceId: "svc-1", memberId: "w1", at: "2026-10-22T08:30:00+00:00" });
    assert.equal(parseCheckout(new URLSearchParams("service=svc-1&with=gone"), svc).memberId, null);
    assert.equal(parseCheckout(new URLSearchParams("service=svc-1&with=w2"), svc).memberId, null); // unnamed: never offered
    const bad = parseCheckout(new URLSearchParams("service=svc-1&at=foo"), svc);
    assert.equal(bad.at, null);
    assert.equal(bad.serviceId, "svc-1");
    assert.equal(parseCheckout(new URL(checkoutUrl("salon", { serviceId: "svc-1", memberId: null, at: null }), "https://x.test").searchParams, svc).memberId, null);
  });

  test("fence: phoneValue joins the chosen dial code unless the number already says its country", () => {
    // Wrong implementations killed: "+31+44...", "+310044..." for a 00 number, dropping the chosen code.
    assert.equal(phoneValue("31", "31", " 06 1234 5678 "), "06 1234 5678");
    assert.equal(phoneValue("44", "31", "07911 123456"), "+4407911 123456");
    assert.equal(phoneValue("31", "31", "+44 7911 123456"), "+44 7911 123456");
    assert.equal(phoneValue("44", "31", "+44 7911 123456"), "+44 7911 123456");
    assert.equal(phoneValue("31", "31", "0044 7911 123456"), "+44 7911 123456");
  });

  test("fence: cleanMessage turns line breaks into LF before dropping control characters", () => {
    // Wrong implementation killed: posting what the backend refuses (tab, CR, zero-width), or dropping
    // control chars first (CR is one, so the line break would vanish).
    assert.equal(cleanMessage("a\tb\u200bc\r\nd\re\u2028f"), "a bc\nd\nef");
    assert.equal(cleanMessage("  "), null);
  });

  test("fence: the dial default follows the business country, which leads the options", () => {
    // Wrong implementation killed: one fixed default, the business country not first, a non-geographic entry.
    for (const [country, dial] of [["NL", "31"], ["PT", "351"], ["BR", "55"], ["GB", "44"], ["US", "1"]]) assert.equal(defaultDial(country), dial);
    assert.match(dialOptions("PT", "en")[0].text, /\(PT \+351\)$/);
    assert.ok(!dialOptions("PT", "en").some((o) => o.region === "001"));
  });

  test("fence: every business country the backend supports has a dialling code", () => {
    // Wrong implementation killed: a new COUNTRIES entry missing from DIAL (the picker would name a wrong country and post a bare number).
    const src = readFileSync(new URL("../../backend/app/countries.py", import.meta.url), "utf8");
    const keys = [...src.slice(src.indexOf("COUNTRIES:")).matchAll(/^\s+"([A-Z]{2})": CountryDefaults/gm)].map((m) => m[1]);
    assert.ok(keys.length >= 5);
    for (const c of keys) assert.notEqual(defaultDial(c), "", c);
  });

  test("fence: the picker search finds a country by dialling code, accent-free name and ISO code", () => {
    // Wrong implementations killed: matching only the name, no accent folding, no ISO match.
    const opts = dialOptions("NL", "en");
    const find = (q) => opts.filter((o) => matchDial(o, q)).map((o) => o.region);
    for (const q of ["44", "+44", "0044"]) assert.ok(find(q).includes("GB"), q);
    // Dialling codes match as a PREFIX of the digits; a substring (`includes`) would also hit these.
    assert.ok(find("21").includes("DZ") && find("+3").includes("NL") && find("+3").includes("IT"));
    assert.ok(!find("13").includes("DZ"));
    assert.ok(!find("1").includes("DZ") && !find("1").includes("LB") && find("1").includes("US"));
    assert.ok(find("sao").includes("ST"));
    assert.ok(find("gb").includes("GB"));
    assert.ok(find("+1").includes("US") && find("+1").includes("CA"));
    assert.deepEqual(find("zzzz"), []);
  });

  test("fence: an empty search lists every country with the business country first; flags come from the ISO code", () => {
    // Wrong implementations killed: a filter that drops on "", a wrong regional-indicator offset.
    const opts = dialOptions("PT", "en");
    assert.equal(opts.filter((o) => matchDial(o, "  ")).length, opts.length);
    assert.equal(opts[0].region, "PT");
    assert.equal(flagOf("NL"), "\u{1F1F3}\u{1F1F1}");
  });

  test("fence: a picked time from the URL survives only if the window still offers it", () => {
    // Wrong implementation killed: trusting a stale `at` (a 409 on Send).
    assert.equal(pickFromUrl("2026-10-22T08:30:00+00:00", ["2026-10-22T08:30:00+00:00"]), "2026-10-22T08:30:00+00:00");
    assert.equal(pickFromUrl("2026-10-22T08:30:00+00:00", ["2026-10-22T09:00:00+00:00"]), null);
    assert.equal(pickFromUrl(null, ["2026-10-22T09:00:00+00:00"]), null);
  });

  test("fence: the first week shown is the Monday of the picked day, never before today", () => {
    // Wrong implementation killed: restoring a pick weeks out into the current week.
    assert.equal(weekFor("2026-11-12", "2026-10-19"), "2026-11-09");
    assert.equal(weekFor("2026-10-22", "2026-10-20"), "2026-10-20");
  });
});

describe("F2 the payment step (ZIF-158)", () => {
  const price = { amount_minor: 3500, currency: "EUR" };

  test("fence: the Element's options equal the server intent's (kills a refused confirm, iDEAL on a check)", () => {
    // Wrong implementation killed: automatic capture or no method list for a check (Stripe refuses the
    // confirm when the Element and the intent differ), a card-only list for a payment, and the
    // paymentMethodTypes name Stripe.js 10 throws on (the step never mounted on dev, 2026-10-22).
    assert.deepEqual(elementsOptions("check", "authorisation", price), {
      mode: "payment", amount: 3500, currency: "eur", captureMethod: "manual", allowedPaymentMethodTypes: ["card"],
    });
    assert.deepEqual(elementsOptions("check", "setup", price), { mode: "setup", currency: "eur", allowedPaymentMethodTypes: ["card"] });
    for (const checkWith of ["authorisation", "setup"]) {
      const pay = elementsOptions("pay", checkWith, price);
      assert.deepEqual(pay, { mode: "payment", amount: 3500, currency: "eur", captureMethod: "automatic" });
      assert.ok(!("allowedPaymentMethodTypes" in pay));
    }
  });

  test("fence: the return URL drops only Stripe's params, never the booking, the pick or the visitor's own", () => {
    // Wrong implementation killed: keeping the whole query (client_secret into history and logs),
    // dropping `booking` (the return could not find its booking), or an allowlist that strips utm_*/gclid.
    const kept = returnParams(
      "?service=s1&with=m1&at=2026-10-22T15%3A00%3A00%2B00%3A00&booking=b1&payment_intent=pi_1&payment_intent_client_secret=pi_1_secret_x&setup_intent=seti_1&setup_intent_client_secret=seti_1_secret_y&redirect_status=succeeded&utm_source=ad",
    );
    assert.equal(kept.toString(), new URLSearchParams({ service: "s1", with: "m1", at: "2026-10-22T15:00:00+00:00", booking: "b1", utm_source: "ad" }).toString());
    assert.equal(returnParams("").toString(), "");
    assert.equal(returnParams("?utm_source=ad&gclid=x").toString(), "utm_source=ad&gclid=x");
  });

  test("fence: payment_required / payment_not_offered reload the options; payments_unavailable is its own outcome", () => {
    // Wrong implementation killed: the generic field banner (the page never reloads its options).
    assert.equal(answerState({ status: 422, code: "payment_required" }).kind, "paymentChanged");
    assert.equal(answerState({ status: 422, code: "payment_not_offered" }).kind, "paymentChanged");
    assert.equal(answerState({ status: 422, code: "invalid_request" }).kind, "fieldErrors");
    assert.equal(answerState({ status: 503, code: "payments_unavailable" }).kind, "paymentsUnavailable");
    assert.equal(answerState({ status: 503, code: "busy" }).kind, "nothingBooked");
  });

  test("fence: payment is sent as given, null when the service has no step", () => {
    // Wrong implementation killed: defaulting to "check" (a 422 on every booking of a page whose step is off).
    const form = { startsAt: "2026-10-20T15:00:00Z", memberId: null, name: "Ana", email: "a@b.nl", phone: "1", locale: "nl", policyVersion: "v", turnstileToken: null, consent: false, message: null };
    assert.equal(bookingBody({ ...form, payment: null }).payment, null);
    assert.equal(bookingBody({ ...form, payment: "check" }).payment, "check");
    assert.equal(bookingBody({ ...form, payment: "pay" }).payment, "pay");
  });

  test("fence: the kept-fee line shows only for Pay now with the flag on; the launch hint says nothing about a fee", () => {
    // Wrong implementation killed: a deduction the client was never told about (flag on), or a kept
    // fee mentioned while nothing is kept (flag off, R6).
    assert.deepEqual(termsLines("pay", true), ["termsPaymentFee"]);
    assert.deepEqual(termsLines("pay", false), []);
    assert.deepEqual(termsLines("check", true), []);
    assert.deepEqual(termsLines(null, true), []);
    const catalogs = Object.fromEntries(["en", "nl", "pt"].map((l) => [l, JSON.parse(readFileSync(new URL(`../messages/${l}.json`, import.meta.url), "utf8")).BookingPage]));
    for (const l of ["en", "nl", "pt"]) for (const key of ["termsPaymentFee", "payNowHint", "payNowHintFee"]) assert.ok(catalogs[l][key], `${l}.${key}`);
    assert.ok(!/fee|kost|taxa|tarifa/i.test(catalogs.en.payNowHint));
    for (const l of ["nl", "pt"]) assert.notEqual(catalogs[l].payNowHint, catalogs[l].payNowHintFee);
    assert.match(catalogs.en.payNowHintFee, /fee/);
    assert.ok(!/kosten/i.test(catalogs.nl.payNowHint) && /kosten/i.test(catalogs.nl.payNowHintFee));
    assert.ok(!/taxa/i.test(catalogs.pt.payNowHint) && /taxa/i.test(catalogs.pt.payNowHintFee));
  });

  test("fence: what the page shows for each booking and payment state", () => {
    // Wrong implementation killed: "You're booked" after a cancelled iDEAL or while processing.
    const sync = (status, state = "open", failed = false) => ({ status, payment: { kind: "pay", state, failed } });
    assert.deepEqual(doneState(sync("pending", "checked")), { kind: "done" });
    assert.deepEqual(doneState(sync("confirmed", "paid")), { kind: "done" });
    assert.deepEqual(doneState(sync("awaiting_payment", "open")), { kind: "resume", failed: false });
    assert.deepEqual(doneState(sync("awaiting_payment", "open", true)), { kind: "resume", failed: true });
    assert.deepEqual(doneState(sync("awaiting_payment", "processing")), { kind: "processing" });
    assert.deepEqual(doneState(sync("expired", "processing")), { kind: "expiredProcessing" });
    assert.deepEqual(doneState(sync("expired", "paid")), { kind: "expiredPaid" });
    assert.deepEqual(doneState(sync("expired", "open")), { kind: "expired" });
    assert.deepEqual(doneState(sync("expired", "canceled")), { kind: "expired" });
    // The server's resume starts a new intent after a cancelled one; paid/checked wait for the webhook.
    assert.deepEqual(doneState(sync("awaiting_payment", "canceled")), { kind: "resume", failed: false });
    assert.deepEqual(doneState(sync("awaiting_payment", "paid")), { kind: "processing" });
    assert.deepEqual(doneState(sync("awaiting_payment", "checked")), { kind: "processing" });
    for (const status of ["declined", "cancelled_by_client", "cancelled_by_business", "completed", "no_show", "something_new"]) {
      assert.deepEqual(doneState(sync(status, "paid")), { kind: "stale" }, status);
    }
  });

  test("fence: the button says it charges the card when Pay now is chosen (CRD 8(2))", () => {
    // Wrong implementation killed: a button that charges the card without saying so.
    assert.equal(sendLabel("pay", true), "payButtonBook");
    assert.equal(sendLabel("pay", false), "payButtonRequest");
    assert.equal(sendLabel("check", true), "book");
    assert.equal(sendLabel("check", false), "request");
    assert.equal(sendLabel(null, true), "book");
    const catalogs = ["en", "nl", "pt"].map((l) => JSON.parse(readFileSync(new URL(`../messages/${l}.json`, import.meta.url), "utf8")).BookingPage);
    assert.deepEqual(catalogs.map((c) => c.payButtonBook), ["Pay {price} and book", "Betaal {price} en boek", "Pagar {price} e reservar"]);
    assert.deepEqual(catalogs.map((c) => c.payButtonRequest), ["Pay {price} and send request", "Betaal {price} en verstuur aanvraag", "Pagar {price} e enviar pedido"]);
  });

  test("fence: the Brazilian page asks Stripe for Brazilian Portuguese", () => {
    // Wrong implementation killed: European Portuguese decline messages on a Brazilian page.
    assert.equal(STRIPE_LOCALE.pt, "pt-BR");
    assert.equal(STRIPE_LOCALE.en, "en");
    assert.equal(STRIPE_LOCALE.nl, "nl");
  });

  test("fence: a booking in the URL resumes whatever else it says; a stored id counts only for its own service", () => {
    // Wrong implementation killed: a refresh or Back that re-runs the pick-from-URL path and drops the
    // client's own time; another service's stored id resumed by mistake; a plain checkout over a
    // booking already paid while away (a second payment).
    const stored = { bookingId: "b1", serviceId: "s1" };
    assert.equal(entryMode(new URLSearchParams("service=s1&booking=b1&at=2026-10-22T15:00:00Z"), null), "resume");
    assert.equal(entryMode(new URLSearchParams("service=s1&booking=b9"), stored), "resume");
    assert.equal(entryMode(new URLSearchParams("service=s1&at=2026-10-22T15:00:00Z"), stored), "stored");
    assert.equal(entryMode(new URLSearchParams("service=s2"), stored), "checkout");
    assert.equal(entryMode(new URLSearchParams("service=s1"), null), "checkout");
    assert.equal(storedNext({ status: "awaiting_payment" }), "offerResume");
    assert.equal(storedNext({ status: "pending" }), "done");
    assert.equal(storedNext({ status: "confirmed" }), "done");
    for (const status of ["expired", "declined", "cancelled_by_client", "completed"]) assert.equal(storedNext({ status }), "checkout", status);
  });

  test("fence: only a settled end state forgets the booking; a failed check keeps it", () => {
    // Wrong implementation killed: checkEmail forgetting the id and the address (the client then books
    // and pays again for another time while the first payment may be landing).
    for (const kind of ["expired", "expiredPaid", "expiredProcessing", "stale"]) assert.equal(mayForget(kind), true, kind);
    assert.equal(mayForget("checkEmail"), false);
    assert.equal(mayForget("processing"), false);
  });

  test("fence: what Send does, from whether it is already working, sent, and holds a secret", () => {
    // Wrong implementation killed: a second press while a send, a kind switch or a verification is in
    // flight (two POSTs, or a confirm of the old intent); a sent request posting again; a retry with no
    // secret doing nothing.
    assert.equal(sendAction({ working: true, sent: false, hasSecret: false }), "ignore");
    assert.equal(sendAction({ working: true, sent: true, hasSecret: true }), "ignore");
    assert.equal(sendAction({ working: false, sent: false, hasSecret: false }), "post");
    assert.equal(sendAction({ working: false, sent: true, hasSecret: true }), "pay");
    assert.equal(sendAction({ working: false, sent: true, hasSecret: false }), "reopen");
  });
});

describe("Turnstile widget", () => {
  test("fence: the booking widget is rendered for the booking action, the one the API checks (ZB-20)", () => {
    // Wrong implementation killed: dropping or renaming the action, so every passing token is refused.
    const source = readFileSync(new URL("../app/[locale]/[slug]/booking-page.tsx", import.meta.url), "utf8");
    const start = source.indexOf("w.render(el, {");
    assert.notEqual(start, -1);
    const options = source.slice(start, source.indexOf("callback:", start));
    assert.match(options, /\baction: "booking",/);
  });
});
