// ZIF-146: the logic behind a client's own account: signing in to it, filling the checkout from it, its bookings
// and the way back to a booking page after an emailed sign-up.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

import {
  accountSignInOutcome,
  afterSignOut,
  applyFill,
  bookAgainSlug,
  emailNotSaved,
  fillFrom,
  historyOutcome,
  historySections,
  historyServiceName,
  linkRequestShape,
  offerNext,
  splitPhone,
  statusPill,
} from "../lib/client-account.ts";

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

describe("filling the checkout from the account", () => {
  const ana = { email: "ana@example.com", name: "Ana Silva", phone: "+31612345678" };

  test("the offer reads the account only when clicked: filled, or sign in first, or a problem", () => {
    assert.equal(offerNext(200), "fill");
    assert.equal(offerNext(401), "signIn");
    assert.equal(offerNext(403), "error");
    assert.equal(offerNext(0), "error");
  });

  test("a phone in the business's country fills in as a local number behind its own code", () => {
    assert.deepEqual(splitPhone("+31612345678", "NL"), { region: "NL", local: "612345678" });
  });

  test("a phone from another country gets that country's code, when only one country has it", () => {
    assert.deepEqual(splitPhone("+351912345678", "NL"), { region: "PT", local: "912345678" });
    assert.deepEqual(splitPhone("+5511912345678", "PT"), { region: "BR", local: "11912345678" });
  });

  test("a code several countries share stays a whole international number: never a guessed country", () => {
    assert.deepEqual(splitPhone("+12025550123", "NL"), { region: "NL", local: "+12025550123" });
    assert.deepEqual(splitPhone("+12025550123", "US"), { region: "US", local: "2025550123" });
  });

  test("no phone fills in no phone", () => {
    assert.equal(splitPhone(null, "NL"), null);
  });

  test("the account's name, email and phone; a field the account has no value for keeps what is typed", () => {
    const fill = fillFrom(ana, "NL");
    assert.deepEqual(fill, { name: "Ana Silva", email: "ana@example.com", phone: "612345678", region: "NL" });
    const typed = { name: "A. S.", email: "typed@example.com", phone: "699", region: "PT" };
    assert.deepEqual(applyFill(typed, fill), { name: "Ana Silva", email: "ana@example.com", phone: "612345678", region: "NL" });
    const bare = fillFrom({ email: "ana@example.com", name: null, phone: null }, "NL");
    assert.deepEqual(applyFill(typed, bare), { name: "A. S.", email: "ana@example.com", phone: "699", region: "PT" });
  });

  test("fence: the checkout reads the account only from the offer's click, never when it loads", () => {
    // Wrong implementation killed: a GET /api/account in an effect, which puts a person's details into every
    // booking page a staff member merely opens (security B3.4).
    const text = readFileSync(new URL("../app/[locale]/[slug]/booking-page.tsx", import.meta.url), "utf8");
    const calls = [...text.matchAll(/platformAccountRead\(/g)];
    assert.equal(calls.length, 1);
    const before = text.slice(0, calls[0].index);
    const opened = before.lastIndexOf("async function fillFromAccount(");
    assert.ok(opened > before.lastIndexOf("useEffect("), "inside the offer's click handler, not an effect");
  });
});

describe("a different email books as a guest (ZIF-146 G1)", () => {
  // The backend proves a booking only with the signed-in account's own email: any other address books as a guest,
  // links nothing and never shows in the account's bookings (tests/test_bookings_api.py pins it).
  const ana = { email: "ana@example.com", name: "Ana Silva", phone: null };
  const page = () => readFileSync(new URL("../app/[locale]/[slug]/booking-page.tsx", import.meta.url), "utf8");
  const fillParts = () => readFileSync(new URL("../app/[locale]/[slug]/account-fill.tsx", import.meta.url), "utf8");

  test("the hint shows only while the email differs from the account's own", () => {
    assert.equal(emailNotSaved(ana, "ana@example.com"), false);
    assert.equal(emailNotSaved(ana, "someone@example.com"), true);
    assert.equal(emailNotSaved(ana, "ana@example.co"), true);
  });

  test("it compares as the server does: case, spaces around it and the Unicode form don't make it another address", () => {
    assert.equal(emailNotSaved(ana, "  Ana@Example.COM "), false);
    assert.equal(emailNotSaved({ ...ana, email: "jos\u00e9@example.com" }, "jose\u0301@example.com"), false);
  });

  test("an empty field gets its own error, not the hint", () => {
    assert.equal(emailNotSaved(ana, ""), false);
    assert.equal(emailNotSaved(ana, "   "), false);
  });

  test("signed out, there is no hint whatever is typed: nothing is compared or looked up", () => {
    for (const typed of ["", "ana@example.com", "someone@example.com"]) assert.equal(emailNotSaved(null, typed), false);
  });

  test("fence: the hint under the email field comes from that comparison and nothing else", () => {
    assert.match(page(), /const notSaved = emailNotSaved\(account, flow\.email\);/);
    assert.match(page(), /\{notSaved && \(\s*<p className=\{styles\.hint\} id="bp-email-account">\s*\{t\("emailNotSaved"\)\}/);
  });

  test("fence: the signed-out offer is the same for any typed value", () => {
    // Wrong implementation killed: an offer that changes with the typed email ("This email has an account, sign
    // in"), which tells a stranger who has an account.
    const props = /export function AccountOffer\(\{ ([^}]*) \}: \{ ([^}]*) \}\)/.exec(fillParts());
    assert.ok(props, "AccountOffer's own props");
    assert.equal(props[1], "t, busy, error, onOpen");
    const uses = [...page().matchAll(/<AccountOffer ([\s\S]*?)\/>/g)].map((m) => m[1]);
    assert.equal(uses.length, 1);
    assert.doesNotMatch(uses[0], /flow|email|name|phone|region|message|marketing/);
  });

  test("the signed-in note says up front that a different email is not saved to the account", () => {
    for (const locale of ["en", "nl", "pt"]) {
      const strings = JSON.parse(readFileSync(new URL(`../messages/${locale}.json`, import.meta.url))).BookingPage;
      assert.ok(strings.filledIn.length > 0 && strings.emailNotSaved.length > 0, locale);
    }
    const en = JSON.parse(readFileSync(new URL("../messages/en.json", import.meta.url))).BookingPage;
    assert.match(en.filledIn, /with a different email it won't be saved to your account/);
    assert.equal(en.emailNotSaved, "This booking won't appear in your bookings.");
  });
});

describe("Not you? Sign out", () => {
  const written = { name: "Ana Silva", email: "ana@example.com", phone: "612345678", region: "NL" };

  test("fields still holding the account's values empty out; the phone's code goes back to the business's", () => {
    assert.deepEqual(afterSignOut({ ...written }, written, "PT"), { name: "", email: "", phone: "", region: "PT" });
  });

  test("whatever the person changed since stays as they typed it", () => {
    const current = { name: "Ana S.", email: "ana@example.com", phone: "699999999", region: "NL" };
    assert.deepEqual(afterSignOut(current, written, "PT"), { name: "Ana S.", email: "", phone: "699999999", region: "NL" });
  });

  test("a phone whose country code was changed is the person's own number now: kept", () => {
    const current = { ...written, region: "BE" };
    assert.deepEqual(afterSignOut(current, written, "PT"), { name: "", email: "", phone: "612345678", region: "BE" });
  });

  test("a field the account never filled in is never touched", () => {
    const partial = { name: null, email: "ana@example.com", phone: null, region: null };
    const current = { name: "Typed Name", email: "ana@example.com", phone: "612", region: "NL" };
    assert.deepEqual(afterSignOut(current, partial, "PT"), { name: "Typed Name", email: "", phone: "612", region: "NL" });
  });

  test("fence: the checkout signs out with DELETE /api/account/session and clears through afterSignOut", () => {
    const text = readFileSync(new URL("../app/[locale]/[slug]/booking-page.tsx", import.meta.url), "utf8");
    assert.match(text, /platformAccountSignOut\(/);
    assert.match(text, /afterSignOut\(/);
  });
});

describe("client sign-up, email first", () => {
  test("an account sign-up link: business sign-up's challenge and limits, and an inbox screen of its own", () => {
    // The backend shares the Turnstile action, the IP key and the per-address key with business sign-up.
    assert.deepEqual(linkRequestShape("account_sign_up"), { action: "sign_up", lede: "clientLede", tooMany: "SignUp" });
  });

  test("the business sign-up and the reset link are unchanged", () => {
    assert.deepEqual(linkRequestShape("sign_up"), { action: "sign_up", lede: "signUpLede", tooMany: "SignUp" });
    assert.deepEqual(linkRequestShape("password_reset"), { action: "password_reset", lede: "resetLede", tooMany: "ForgotPassword" });
  });

  test("both sign-in pages offer to create an account", () => {
    const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
    assert.match(read("../app/[locale]/sign-in/sign-in-form.tsx"), /\/account\/sign-up/);
    assert.match(read("../app/[locale]/account/sign-in/account-sign-in-page.tsx"), /href="\/account\/sign-up"/);
  });
});
