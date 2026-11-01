// ZIF-146: the logic behind a client's own account: signing in to it, filling the checkout from it, its bookings
// and the way back to a booking page after an emailed sign-up.
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, test } from "node:test";

import {
  accountPhone,
  accountHeld,
  accountSignInOutcome,
  afterAccountSignOut,
  afterFill,
  afterUnlock,
  afterSignOut,
  applyFill,
  bookAgainSlug,
  completeOutcome,
  dialRegion,
  emailNotSaved,
  fillFrom,
  forgetStaleReturn,
  filledForm,
  historyOutcome,
  historyServiceName,
  historyWhen,
  mergeHistory,
  linkRequestShape,
  offerNext,
  recheckOutcome,
  rememberReturn,
  sectionAfterPage,
  saveDetailsOffer,
  RETURN_KEY,
  splitPhone,
  SIGNED_OUT,
  signedOutForm,
  statusPill,
  takeReturn,
} from "../lib/client-account.ts";
import { accountStore, signOutWith } from "../lib/account-store.ts";

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

// Forty-five minutes long, like the mock's haircut.
const booking = (startsAt, status = "confirmed", slug = "salon-bloom") => ({
  business: { name: "Salon Bloom", slug, timezone: "Europe/Amsterdam" },
  starts_at: startsAt,
  ends_at: new Date(new Date(startsAt).getTime() + 45 * 60_000).toISOString().replace(".000Z", "Z"),
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

  test("the server decides the sections: what it answers is shown as it comes, in its order", () => {
    // PR 6 fold, concern 3: the API's section=upcoming (soonest first) and section=past (newest first) replace
    // the page's own split and its "keep loading" loop (backend tests pin the rule).
    const one = { bookings: [booking("2026-11-04T09:00:00Z")], next: "c1" };
    const after = sectionAfterPage({ bookings: [], next: null }, one, null);
    assert.deepEqual(after, { bookings: one.bookings, next: "c1" });
    const two = { bookings: [booking("2026-11-05T09:00:00Z")], next: null };
    assert.deepEqual(sectionAfterPage(after, two, "c1"), { bookings: [...one.bookings, ...two.bookings], next: null });
  });

  test("every past row offers Book again", () => {
    for (const st of ["completed", "cancelled_by_client", "declined", "expired", "no_show"]) {
      assert.equal(bookAgainSlug(booking("2026-09-06T09:00:00Z", st), false), "salon-bloom");
    }
  });

  test("Show more appends the next page; a first page replaces what was shown", () => {
    const one = [booking("2026-11-04T09:00:00Z")];
    const two = [booking("2026-09-01T09:00:00Z")];
    assert.deepEqual(mergeHistory(one, two, "cursor"), [...one, ...two]);
    assert.deepEqual(mergeHistory(one, two, null), two);
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

  test("the offer reads the account only when clicked: confirm who, or sign in first, or a problem", () => {
    assert.equal(offerNext(200), "confirm");
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
    // Two reads, each inside its own handler: the offer's click, and the re-check of an account the person already
    // filled in from (security review 6), which returns at once when nothing was filled in.
    const calls = [...text.matchAll(/platformAccountRead\(/g)];
    assert.equal(calls.length, 2);
    const handlers = ["async function fillFromAccount(", "async function recheckAccount("];
    calls.forEach((call, i) => {
      const before = text.slice(0, call.index);
      const opened = before.lastIndexOf(handlers[i]);
      assert.ok(opened > before.lastIndexOf("useEffect("), `${handlers[i]}: not an effect`);
      assert.doesNotMatch(before.slice(opened + handlers[i].length), /\n {2}(?:async )?function /, `${handlers[i]}: inside it`);
    });
    assert.match(text, /async function recheckAccount\(\): Promise<boolean> \{\s*if \(!account\) return true;/);
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
    assert.match(text, /await signOutAccount\(\)/);
    assert.match(text, /signedOutForm\(/);
    const button = readFileSync(new URL("../app/[locale]/_ui/account-button.tsx", import.meta.url), "utf8");
    assert.match(button, /platformAccountSignOut\(/);
  });

  test("fence: a request already sent keeps the details it went with when the account signs out", () => {
    // Wrong implementation killed: emptying the locked fields of a request waiting for its payment, whose resend
    // and done screen read them.
    const text = readFileSync(new URL("../app/[locale]/[slug]/booking-page.tsx", import.meta.url), "utf8");
    assert.match(text, /const \{ state, clear \} = afterAccountSignOut\(acct, heldNow\(\)\);\s*if \(clear\) setFlow\(\(f\) => signedOutForm\(/);
  });
});

describe("client sign-up, email first", () => {
  test("an account sign-up link: business sign-up's challenge and limits, and an inbox screen of its own", () => {
    // The backend shares the Turnstile action, the IP key and the per-address key with business sign-up.
    assert.deepEqual(linkRequestShape("account_sign_up"), { action: "sign_up", lede: "clientLede", tooMany: "SignUp", endpoint: "platformAccountSignUp" });
  });

  test("the business sign-up and the reset link are unchanged", () => {
    assert.deepEqual(linkRequestShape("sign_up"), { action: "sign_up", lede: "signUpLede", tooMany: "SignUp", endpoint: "accountSignUp" });
    assert.deepEqual(linkRequestShape("password_reset"), { action: "password_reset", lede: "resetLede", tooMany: "ForgotPassword", endpoint: "accountRequestPasswordReset" });
  });

  test("fence: the link request calls the endpoint its shape names (PR 6 review, correctness 11, M1)", () => {
    // Wrong implementation killed: account sign-up calling the business sign-up, which emails a business link.
    const text = readFileSync(new URL("../app/[locale]/_ui/link-request.tsx", import.meta.url), "utf8");
    assert.match(text, /const call = ENDPOINTS\[shape\.endpoint\];/);
    assert.match(text, /const ENDPOINTS = \{ accountRequestPasswordReset, accountSignUp, platformAccountSignUp \} as const;/);
  });

  test("both sign-in pages offer to create an account", () => {
    const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
    assert.match(read("../app/[locale]/sign-in/sign-in-form.tsx"), /\/account\/sign-up/);
    assert.match(read("../app/[locale]/account/sign-in/account-sign-in-page.tsx"), /href="\/account\/sign-up"/);
  });
});

describe("finishing the account from the link", () => {
  test("its answers: done, the link spent or expired, the email already has an account, or a field to fix", () => {
    assert.equal(completeOutcome(201, undefined), "done");
    assert.equal(completeOutcome(400, "invalid_token"), "expired");
    assert.equal(completeOutcome(409, "account_exists"), "exists");
    assert.equal(completeOutcome(422, "password_too_short"), "password");
    assert.equal(completeOutcome(422, "password_too_long"), "password");
    assert.equal(completeOutcome(422, "password_too_common"), "password");
    assert.equal(completeOutcome(422, "phone_invalid"), "phone");
    assert.equal(completeOutcome(422, "invalid_request"), "error");
    assert.equal(completeOutcome(409, "setup_required"), "error");
    assert.equal(completeOutcome(503, "busy"), "error");
  });

  test("the phone is optional and always goes out international: the account has no country to read it in", () => {
    assert.equal(accountPhone("NL", ""), null);
    assert.equal(accountPhone("NL", "   "), null);
    assert.equal(accountPhone("NL", "6 1234 5678"), "+316 1234 5678");
    assert.equal(accountPhone("PT", "+351 912 345 678"), "+351 912 345 678");
    assert.equal(accountPhone("BR", "0055 11 91234 5678"), "+55 11 91234 5678");
  });

  test("the country code starts at the browser's own country, else the page language's", () => {
    assert.equal(dialRegion(["en-US", "en"], "en"), "US");
    assert.equal(dialRegion(["pt-PT"], "pt"), "PT");
    assert.equal(dialRegion(["en"], "en"), "GB");
    assert.equal(dialRegion([], "nl"), "NL");
    assert.equal(dialRegion(["pt"], "pt"), "BR");
    assert.equal(dialRegion(["en-XX"], "en"), "GB");
  });
});

describe("back to the booking page after an emailed sign-up (ruling 7: this browser only)", () => {
  function memory() {
    const items = new Map();
    return {
      items,
      getItem: (key) => (items.has(key) ? items.get(key) : null),
      setItem: (key, value) => items.set(key, String(value)),
      removeItem: (key) => items.delete(key),
    };
  }
  const t0 = 1_000_000;

  test("the booking page's slug is kept, and only that: never an email or a path", () => {
    const store = memory();
    rememberReturn(() => store, "salon-bloom", t0);
    assert.deepEqual(JSON.parse(store.items.get(RETURN_KEY)), { slug: "salon-bloom", at: t0 });
  });

  test("taken once, within the link's 24 hours", () => {
    const store = memory();
    rememberReturn(() => store, "salon-bloom", t0);
    assert.equal(takeReturn(() => store, t0 + 23 * 3_600_000), "salon-bloom");
    assert.equal(takeReturn(() => store, t0 + 23 * 3_600_000), null);
    rememberReturn(() => store, "salon-bloom", t0);
    assert.equal(takeReturn(() => store, t0 + 24 * 3_600_000 + 1), null);
    assert.equal(store.items.size, 0);
  });

  test("anything else stored there leads nowhere: the way back is a same-origin booking page or nothing", () => {
    for (const value of ['{"slug":"//evil.example","at":1000000}', '{"slug":"/x","at":1000000}', '{"slug":"javascript:alert(1)","at":1000000}', "junk", '{"at":1000000}']) {
      const store = memory();
      store.setItem(RETURN_KEY, value);
      assert.equal(takeReturn(() => store, t0), null, value);
    }
    const store = memory();
    rememberReturn(() => store, "../evil", t0);
    assert.equal(store.items.size, 0);
  });

  test("a browser that refuses storage just has no way back", () => {
    const refusing = { getItem() { throw new Error("denied"); }, setItem() { throw new Error("denied"); }, removeItem() { throw new Error("denied"); } };
    assert.doesNotThrow(() => rememberReturn(() => refusing, "salon-bloom", t0));
    assert.equal(takeReturn(() => refusing, t0), null);
    // Reading window.localStorage itself throws where site data is blocked.
    const blocked = () => {
      throw new Error("SecurityError");
    };
    assert.doesNotThrow(() => rememberReturn(blocked, "salon-bloom", t0));
    assert.equal(takeReturn(blocked, t0), null);
  });

  test("fence: the done screen offers the account only for a guest booking this page placed, and remembers the way back", () => {
    const page = readFileSync(new URL("../app/[locale]/[slug]/booking-page.tsx", import.meta.url), "utf8");
    assert.match(page, /saveDetails=\{offerAccount\}/);
    const done = readFileSync(new URL("../app/[locale]/[slug]/checkout.tsx", import.meta.url), "utf8");
    assert.match(done, /rememberReturn\(\(\) => window\.localStorage, slug, Date\.now\(\)\)/);
  });
});

// What the console may never contain: a link to the account pages (quoted, or after an interpolated locale), a
// read of a person's own account or history, or the account's own parts.
const CONSOLE_REFUSES = /(?:["'`]|\})\/account\b|\bplatformAccount(?:Read|History)\b|\bshownAccount\b|\bAccountSignIn\b|\bAccountButton\b/;

describe("the account button", () => {
  const ana = { email: "ana@example.com", name: "Ana Silva", phone: null };

  test("it shows the account this page signed in or filled in from, and nothing before", () => {
    const store = accountStore();
    const seen = [];
    store.subscribe(() => seen.push(store.get()));
    assert.equal(store.get(), null);
    store.show(ana);
    assert.deepEqual(seen, [ana]);
  });

  test("a sign-out from the menu empties it and tells the page, which empties what the account filled in", () => {
    const store = accountStore();
    let told = 0;
    const stop = store.onSignedOut(() => (told += 1));
    store.show(ana);
    store.signedOut();
    assert.equal(store.get(), null);
    assert.equal(told, 1);
    stop();
    store.signedOut();
    assert.equal(told, 1);
  });

  test("fence: the console never links to the account pages", () => {
    // Personal pages are never reachable from the console (ZIF-146 handover): the account button lives on the
    // booking and account pages only.
    const root = new URL("../app/[locale]/", import.meta.url).pathname;
    const walk = (dir) => readdirSync(dir).flatMap((n) => (statSync(join(dir, n)).isDirectory() ? walk(join(dir, n)) : [join(dir, n)]));
    for (const file of [...walk(join(root, "(console)")), join(root, "_ui", "console.tsx")]) {
      assert.doesNotMatch(readFileSync(file, "utf8"), CONSOLE_REFUSES, file);
    }
  });

  test("the console check catches every way in (security review 2)", () => {
    // A link built with the locale, as the sign-in page's own link is, and the account's reads and parts.
    for (const probe of [
      "<a href={`/${locale}/account`}>x</a>",
      '<Link href="/account">x</Link>',
      "router.push('/account/sign-in')",
      "send(platformAccountRead())",
      "send(platformAccountHistory({ query: {} }))",
      "shownAccount.show(me)",
      "<AccountSignIn />",
      "<AccountButton />",
    ]) {
      assert.match(probe, CONSOLE_REFUSES, probe);
    }
    for (const fine of ['fetch("/api/account-settings")', "const accountName = 1;", "<AccountPopover />"]) assert.doesNotMatch(fine, CONSOLE_REFUSES, fine);
  });
});

describe("your bookings, at each business's own time (PR 6 review, correctness 1)", () => {
  test("a time at the business's own zone, as its booking page, emails and manage page show it", () => {
    // 09:00 UTC is 10:00 in Amsterdam and 09:00 in Lisbon.
    assert.deepEqual(historyWhen("2026-10-31T09:00:00Z", "Europe/Amsterdam", "Europe/Amsterdam", "en", true), { text: "Sat 31 Oct, 10:00", city: null });
    assert.deepEqual(historyWhen("2026-10-31T09:00:00Z", "Europe/Lisbon", "Europe/Amsterdam", "en", true), { text: "Sat 31 Oct, 09:00", city: "Lisbon" });
  });

  test("the city is named only when the business's zone is not the reader's", () => {
    assert.equal(historyWhen("2026-10-31T09:00:00Z", "Europe/Lisbon", "Europe/Lisbon", "en", true).city, null);
    assert.equal(historyWhen("2026-10-31T09:00:00Z", "America/Sao_Paulo", "Europe/Lisbon", "pt", true).city, "Sao Paulo");
  });

  test("a past row's day is the business's day, near midnight too", () => {
    // 23:30 UTC on 31 Oct is still the 31st in Lisbon and already 1 Nov in Amsterdam.
    assert.deepEqual(historyWhen("2026-10-31T23:30:00Z", "Europe/Lisbon", "Europe/Amsterdam", "en", false), { text: "Sat 31 Oct", city: "Lisbon" });
  });

  test("fence: the page formats each row with its business's zone and the reader's", () => {
    const text = readFileSync(new URL("../app/[locale]/account/your-bookings.tsx", import.meta.url), "utf8");
    assert.match(text, /historyWhen\(b\.starts_at, b\.business\.timezone, readerZone, locale, ahead\)/);
    assert.doesNotMatch(text, /new Intl\.DateTimeFormat/);
  });
});

describe("a client on the console's sign-in (PR 6 review, correctness 2)", () => {
  test("fence: no business after a correct password points to the account sign-in, not to a dead end", () => {
    // Wrong implementation killed: every 403 read as "isn't part of a business any more", which a client who
    // reset their password then reads with no way on.
    const text = readFileSync(new URL("../app/[locale]/sign-in/sign-in-form.tsx", import.meta.url), "utf8");
    const branch = /\} else if \(outcome\.status === 403 && outcome\.code === "no_tenant"\) \{([\s\S]*?)\n {4}\}/.exec(text);
    assert.ok(branch, "a no_tenant branch");
    assert.match(branch[1], /t\.rich\("noTenant"/);
    assert.match(branch[1], /href=\{`\/\$\{locale\}\/account\/sign-in`\}/);
    for (const locale of ["en", "nl", "pt"]) {
      const strings = JSON.parse(readFileSync(new URL(`../messages/${locale}.json`, import.meta.url))).SignIn;
      assert.match(strings.noTenant, /<account>.+<\/account>/, locale);
    }
  });
});

describe("the account controls wait while a booking is on its way (PR 6 review, correctness 3)", () => {
  const ana = { email: "ana@example.com", name: "Ana Silva", phone: "+31612345678" };
  const fill = { name: "Ana Silva", email: "ana@example.com", phone: "612345678", region: "NL" };
  const free = { locked: false, busy: false, verifying: false, working: false };

  test("held from Send to the answer: verifying, sending, working, or locked waiting for its payment", () => {
    assert.equal(accountHeld(free), false);
    for (const key of ["locked", "busy", "verifying", "working"]) assert.equal(accountHeld({ ...free, [key]: true }), true, key);
  });

  test("a fill while held changes nothing: the request goes with the details it was sent with", () => {
    assert.equal(afterFill(SIGNED_OUT, ana, fill, true), SIGNED_OUT);
    assert.deepEqual(afterFill(SIGNED_OUT, ana, fill, false), { account: ana, written: fill, pending: null });
  });

  test("a sign-out while free empties what the account wrote; while held it empties nothing yet", () => {
    const filled = { account: ana, written: fill, pending: null };
    assert.deepEqual(afterAccountSignOut(filled, false), { state: SIGNED_OUT, clear: fill });
    assert.equal(afterAccountSignOut(filled, true).clear, null);
    assert.equal(afterAccountSignOut(filled, true).state.account, null);
  });

  test("fence: the offer and \"Not you?\" wait while held, and fill and the sign-out listener ask heldNow()", () => {
    const text = readFileSync(new URL("../app/[locale]/[slug]/booking-page.tsx", import.meta.url), "utf8");
    assert.match(text, /const held = accountHeld\(\{ locked, busy: flow\.busy, verifying, working: false \}\);/);
    assert.match(text, /!held && <AccountOffer /);
    assert.match(text, /<SignedInNote [^>]*busy=\{signOutBusy \|\| held\}/);
    assert.match(text, /function fill\(signedIn: AccountOut\) \{\s*\/\/[^\n]*\n\s*if \(heldNow\(\)\) return;/);
    assert.match(text, /afterAccountSignOut\(acct, heldNow\(\)\)/);
  });
});

describe("a sign-out while held empties the form once it unlocks (PR 6 review, correctness 4)", () => {
  const ana = { email: "ana@example.com", name: "Ana Silva", phone: null };
  const fill = { name: "Ana Silva", email: "ana@example.com", phone: null, region: null };

  test("held: the clear waits; unlocked (Change time or details): it is applied, once", () => {
    // Wrong implementation killed: forgetting what the account wrote, so the next person on a shared device gets
    // the previous one's details back, editable, when the request is released.
    const { state } = afterAccountSignOut({ account: ana, written: fill, pending: null }, true);
    assert.deepEqual(state, { account: null, written: null, pending: fill });
    const unlocked = afterUnlock(state);
    assert.deepEqual(unlocked, { state: SIGNED_OUT, clear: fill });
    assert.deepEqual(afterUnlock(unlocked.state), { state: SIGNED_OUT, clear: null });
  });

  test("a fill after it replaces the pending clear: the new account's values are the ones to empty", () => {
    const pending = { account: null, written: null, pending: fill };
    const other = { name: "Bo", email: "bo@example.com", phone: null, region: null };
    assert.deepEqual(afterFill(pending, ana, other, false), { account: ana, written: other, pending: null });
  });

  test("fence: every way the checkout unlocks applies the pending clear", () => {
    const text = readFileSync(new URL("../app/[locale]/[slug]/booking-page.tsx", import.meta.url), "utf8");
    const reset = /function reset\(\) \{([\s\S]*?)\n {2}\}/.exec(text);
    assert.ok(reset);
    assert.match(reset[1], /unlockAccount\(\);/);
    assert.match(text, /function unlockAccount\(\) \{\s*const \{ state, clear \} = afterUnlock\(acct\);/);
  });
});

describe("a fill lands on the form as it is now (PR 6 review, correctness 6)", () => {
  const form = (over = {}) => ({ name: "", email: "", phone: "", region: "NL", errors: {}, suggest: null, step: 3, ...over });

  test("what the account has no value for keeps what was typed meanwhile", () => {
    // Wrong implementation killed: a fill computed from the form as it was when the offer was clicked, which puts
    // back an empty phone over the one typed while the account was being read.
    const typedMeanwhile = form({ phone: "699123456", region: "BE" });
    const fill = { name: "Ana Silva", email: "ana@example.com", phone: null, region: null };
    const next = filledForm(typedMeanwhile, fill);
    assert.deepEqual([next.name, next.email, next.phone, next.region, next.step], ["Ana Silva", "ana@example.com", "699123456", "BE", 3]);
  });

  test("only the errors of the fields it wrote are cleared", () => {
    const withErrors = form({ errors: { name: "x", email: "y", phone: "z", message: "m" } });
    const noPhone = filledForm(withErrors, { name: "Ana", email: "ana@example.com", phone: null, region: null });
    assert.deepEqual(noPhone.errors, { name: undefined, email: undefined, phone: "z", message: "m" });
    const noName = filledForm(withErrors, { name: null, email: "ana@example.com", phone: "612", region: "NL" });
    assert.deepEqual(noName.errors, { name: "x", email: undefined, phone: undefined, message: "m" });
  });

  test("a sign-out's clear also works on the form as it is now", () => {
    const fill = { name: "Ana Silva", email: "ana@example.com", phone: "612", region: "NL" };
    const next = signedOutForm(form({ name: "Ana Silva", email: "typed@example.com", phone: "612", region: "NL", suggest: "x" }), fill, "PT");
    assert.deepEqual([next.name, next.email, next.phone, next.region, next.suggest], ["", "typed@example.com", "", "PT", null]);
  });

  test("fence: fill and the clears update the form through its current state; a sign-in after the dialog closed fills nothing", () => {
    const text = readFileSync(new URL("../app/[locale]/[slug]/booking-page.tsx", import.meta.url), "utf8");
    assert.match(text, /setFlow\(\(f\) => filledForm\(f, filled\)\)/);
    assert.equal([...text.matchAll(/setFlow\(\(f\) => signedOutForm\(f, clear, page\.country\)\)/g)].length, 2);
    assert.doesNotMatch(text, /setRegion|useState<string>\(page\.country\)/);
    assert.match(text, /onSignedIn=\{fillFromDialog\}/);
    assert.match(text, /function fillFromDialog\(signedIn: AccountOut\) \{\s*\/\/[^\n]*\n\s*if \(signInOpenRef\.current\) fill\(signedIn\);/);
  });
});

describe("an account already signed in is confirmed, never filled silently (security review 4, owner may overrule)", () => {
  const text = () => readFileSync(new URL("../app/[locale]/[slug]/booking-page.tsx", import.meta.url), "utf8");

  test("fence: the offer's click only asks \"Continue as {name}?\"; the form fills on Continue", () => {
    // Wrong implementation killed: a click on "Sign in to fill in your details" that fills the details of whoever
    // is signed in on a shared device, with no step between.
    const handler = /async function fillFromAccount\(\) \{([\s\S]*?)\n {2}\}/.exec(text());
    assert.ok(handler);
    assert.doesNotMatch(handler[1], /\bfill\(/);
    assert.match(handler[1], /if \(nextStep === "confirm" && answer\.data\) \{\s*setConfirming\(answer\.data\);\s*setSignInOpen\(true\);/);
    assert.match(text(), /onContinue=\{\(\) => confirming && fillFromDialog\(confirming\)\}/);
    assert.match(text(), /onSomeoneElse=\{\(\) => setConfirming\(null\)\}/);
  });

  test("the confirm step names only the person; the strings exist in every locale", () => {
    const parts = readFileSync(new URL("../app/[locale]/[slug]/account-fill.tsx", import.meta.url), "utf8");
    assert.match(parts, /t\("continueTitle", \{ name: confirm\.name \|\| confirm\.email \}\)/);
    assert.doesNotMatch(parts, /confirm\.phone/);
    for (const locale of ["en", "nl", "pt"]) {
      const strings = JSON.parse(readFileSync(new URL(`../messages/${locale}.json`, import.meta.url))).BookingPage;
      for (const key of ["continueTitle", "continueAction", "someoneElse"]) assert.ok(strings[key], `${locale} ${key}`);
    }
  });
});

describe("an account that ended elsewhere is noticed (security review 6)", () => {
  const ana = { email: "ana@example.com", name: "Ana Silva", phone: null };

  test("still the same account; ended (signed out, or someone else now); or not known (no answer): kept", () => {
    assert.equal(recheckOutcome(200, { ...ana, email: " ANA@example.com" }, ana), "same");
    assert.equal(recheckOutcome(401, undefined, ana), "ended");
    assert.equal(recheckOutcome(200, { ...ana, email: "bo@example.com" }, ana), "ended");
    assert.equal(recheckOutcome(0, undefined, ana), "unknown");
    assert.equal(recheckOutcome(503, undefined, ana), "unknown");
  });

  test("ended: the account goes from the header, and no page empties the person's own fields", () => {
    const store = accountStore();
    let told = 0;
    store.onSignedOut(() => (told += 1));
    store.show(ana);
    store.ended();
    assert.equal(store.get(), null);
    assert.equal(told, 0);
  });

  test("fence: re-checked when the page regains focus and before a booking is sent", () => {
    const text = readFileSync(new URL("../app/[locale]/[slug]/booking-page.tsx", import.meta.url), "utf8");
    assert.match(text, /window\.addEventListener\("focus", onFocus\)/);
    assert.match(text, /if \(account && !\(await recheckAccount\(\)\)\) \{/);
    assert.match(text, /text: t\("accountEnded"\)/);
  });
});

describe("the done screen never asks someone signed in to create an account (PR 6 review 5, fold concern 2)", () => {
  const offer = (placedHere, placedByAccount, signedInOnPage) => saveDetailsOffer({ placedHere, placedByAccount, signedInOnPage });

  test("offered only for a booking this page placed that the server says no account placed", () => {
    assert.equal(offer(true, false, false), true);
    // Signed in on this browser without ever using the offer, and booked with the account's own email: the server
    // proved it and recorded it as the account's.
    assert.equal(offer(true, true, false), false);
    // Filled in from an account on this page: that person has one, whatever email they booked with (G1).
    assert.equal(offer(true, false, true), false);
    // A redirect payment (3-D Secure, iDEAL) comes back in a new page load with no answer to read: not offered.
    assert.equal(offer(false, false, false), false);
  });

  test("fence: both ways to the done screen decide it from the booking's own answer, never from reading the account", () => {
    const page = readFileSync(new URL("../app/[locale]/[slug]/booking-page.tsx", import.meta.url), "utf8");
    assert.match(page, /placed\.current = \{ bookingId: id, email: f\.email, placedByAccount: answer\.data\.placed_by_account \?\? false, signedIn: account !== null \};/);
    assert.match(page, /saveDetailsOffer\(\{\s*placedHere: placed\.current\?\.bookingId === s\.id,\s*placedByAccount: placed\.current\?\.placedByAccount \?\? true,\s*signedInOnPage: placed\.current\?\.signedIn \?\? true,\s*\}\)/);
    assert.match(page, /saveDetailsOffer\(\{ placedHere: true, placedByAccount: answer\.data\.placed_by_account \?\? false, signedInOnPage: account !== null \}\)/);
    // The done screen itself reads nothing about the person.
    const done = readFileSync(new URL("../app/[locale]/[slug]/checkout.tsx", import.meta.url), "utf8");
    assert.doesNotMatch(done, /platformAccount|shownAccount|sessionRead/);
  });
});

describe("pt on the client's pages says reserva (PR 6 review, correctness 7)", () => {
  const pt = JSON.parse(readFileSync(new URL("../messages/pt.json", import.meta.url)));

  test("the account pages and the checkout's hint say reservas, never the console's agendamentos", () => {
    const strings = [...Object.values(pt.ClientHome), ...Object.values(pt.ClientSignUp), pt.BookingPage.emailNotSaved, pt.SignIn.noTenant];
    for (const text of strings) assert.doesNotMatch(text, /agendament/i, text);
  });

  test("headings and pills agree with the feminine noun", () => {
    assert.equal(pt.ClientHome.title, "Suas reservas");
    assert.deepEqual(
      [pt.ClientHome.upcoming, pt.ClientHome.confirmed, pt.ClientHome.cancelled, pt.ClientHome.declined, pt.ClientHome.expired],
      ["Próximas", "Confirmada", "Cancelada", "Recusada", "Expirada"],
    );
  });
});

describe("the bookings page loads each section from the server (PR 6 review 8, fold concern 3)", () => {
  test("fence: one request per page of a section, merged through sectionAfterPage; no client split, no loop", () => {
    const text = readFileSync(new URL("../app/[locale]/account/your-bookings.tsx", import.meta.url), "utf8");
    assert.match(text, /platformAccountHistory\(\{ query: \{ section: which, limit: PAGE, \.\.\.\(cursor \? \{ cursor \} : \{\}\) \} \}\)/);
    assert.match(text, /setSections\(\(shown\) => \(\{ \.\.\.shown, \[which\]: sectionAfterPage\(shown\[which\], page, cursor\) \}\)\)/);
    assert.doesNotMatch(text, /historySections|needsMore|for \(;;\)|while \(/);
    assert.match(text, /loadSection\("upcoming", null\)/);
    assert.match(text, /loadSection\("past", null\)/);
  });
});

describe("the finish screen's country picker keeps its order (PR 6 review, correctness 9)", () => {
  test("fence: the list is ordered from the starting country, not from each pick", () => {
    // Wrong implementation killed: dialOptions(region, ...) with the picked region, which moves each pick to the top
    // of the list, under the cursor.
    const text = readFileSync(new URL("../app/[locale]/account/complete/complete-account.tsx", import.meta.url), "utf8");
    assert.match(text, /const \[startRegion\] = useState\(\(\) => dialRegion\(navigator\.languages \?\? \[\], locale\)\);/);
    assert.match(text, /const \[region, setRegion\] = useState\(startRegion\);/);
    assert.match(text, /useMemo\(\(\) => dialOptions\(startRegion, locale\), \[startRegion, locale\]\)/);
  });
});

describe("signing out empties the bookings at once (PR 6 review, correctness 10)", () => {
  test("fence: the list listens for the sign-out and empties itself, before the sign-in page loads", () => {
    const text = readFileSync(new URL("../app/[locale]/account/your-bookings.tsx", import.meta.url), "utf8");
    assert.match(text, /useEffect\(\s*\(\) =>\s*shownAccount\.onSignedOut\(\(\) => \{\s*setSections\(NOTHING\);\s*setAccount\(null\);/);
  });
});

describe("the way back is gone after its 24 hours, read or not (security review 3)", () => {
  function memory(value) {
    const items = new Map(value === undefined ? [] : [[RETURN_KEY, value]]);
    return { items, getItem: (k) => items.get(k) ?? null, setItem: (k, v) => items.set(k, v), removeItem: (k) => items.delete(k) };
  }
  const t0 = 1_000_000;

  test("an entry past its 24 hours, or one that is not ours, is removed; a fresh one stays for the sign-up", () => {
    // Wrong implementation killed: expiring only on read, which leaves which business someone booked with on a
    // shared device for good when the sign-up is finished elsewhere.
    const stale = memory(JSON.stringify({ slug: "therapist", at: t0 }));
    forgetStaleReturn(() => stale, t0 + 24 * 3_600_000 + 1);
    assert.equal(stale.items.size, 0);
    const fresh = memory(JSON.stringify({ slug: "therapist", at: t0 }));
    forgetStaleReturn(() => fresh, t0 + 3_600_000);
    assert.equal(fresh.items.size, 1);
    for (const junk of ["junk", '{"slug":1,"at":1}', '{"slug":"//x","at":1000000}']) {
      const store = memory(junk);
      forgetStaleReturn(() => store, t0);
      assert.equal(store.items.size, 0, junk);
    }
    const empty = memory();
    assert.doesNotThrow(() => forgetStaleReturn(() => empty, t0));
    assert.doesNotThrow(() => forgetStaleReturn(() => { throw new Error("SecurityError"); }, t0));
  });

  test("fence: every page load prunes it", () => {
    const layout = readFileSync(new URL("../app/[locale]/layout.tsx", import.meta.url), "utf8");
    assert.match(layout, /<ReturnPrune \/>/);
    const prune = readFileSync(new URL("../app/[locale]/_ui/return-prune.tsx", import.meta.url), "utf8");
    assert.match(prune, /useEffect\(\(\) => forgetStaleReturn\(\(\) => window\.localStorage, Date\.now\(\)\), \[\]\);/);
  });
});

describe("the finish screen loads no third-party script while a usable link is on the page (security review 5)", () => {
  test("fence: the form renders no Turnstile; only the screen that asks for a new link does", () => {
    const text = readFileSync(new URL("../app/[locale]/account/complete/complete-account.tsx", import.meta.url), "utf8");
    const form = text.slice(text.lastIndexOf('<h1 className={styles.heading}>{t("completeTitle")}</h1>'));
    assert.ok(form.includes("<form"));
    assert.doesNotMatch(form, /<Turnstile|<LinkRequest/);
    const page = readFileSync(new URL("../app/[locale]/account/complete/page.tsx", import.meta.url), "utf8");
    assert.match(page, /no third-party script while a usable link is on the page/);
  });
});

describe("the account sign-in says whose sign-in it is (security review 7, owner may overrule)", () => {
  test("fence: a lede for client accounts and a way to the business sign-in, in every locale", () => {
    const text = readFileSync(new URL("../app/[locale]/account/sign-in/account-sign-in-page.tsx", import.meta.url), "utf8");
    assert.match(text, /<p className=\{styles\.lede\}>\{t\("accountLede"\)\}<\/p>/);
    assert.match(text, /t\.rich\("businessInstead", \{ business: \(chunks\) => <Link href="\/sign-in">\{chunks\}<\/Link> \}\)/);
    for (const locale of ["en", "nl", "pt"]) {
      const strings = JSON.parse(readFileSync(new URL(`../messages/${locale}.json`, import.meta.url))).SignIn;
      assert.ok(strings.accountLede, locale);
      assert.match(strings.businessInstead, /<business>.+<\/business>/, locale);
    }
  });
});

describe("a failed sign-out keeps the account shown (PR 6 review, correctness 11, M13)", () => {
  const ana = { email: "ana@example.com", name: "Ana Silva" };

  test("only a 204 ends it on the page: anything else leaves the session alive, and the page says so", async () => {
    // Wrong implementation killed: clearing on any answer, which shows signed out while the session lives on and the
    // next booking with that email is still proven by it.
    for (const status of [0, 415, 500, 503]) {
      const store = accountStore();
      store.show(ana);
      const answer = await signOutWith(async () => ({ status }), store);
      assert.equal(answer.status, status);
      assert.equal(store.get(), ana, String(status));
    }
    const store = accountStore();
    store.show(ana);
    await signOutWith(async () => ({ status: 204 }), store);
    assert.equal(store.get(), null);
  });

  test("fence: the account button signs out through it", () => {
    const button = readFileSync(new URL("../app/[locale]/_ui/account-button.tsx", import.meta.url), "utf8");
    assert.match(button, /return signOutWith\(\(\) => send\(platformAccountSignOut\(\{ \.\.\.JSON_WRITE \}\)\), shownAccount\);/);
  });
});

describe("the wiring the review's mutations got through (PR 6 review, correctness 11)", () => {
  const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");

  test("M2, M3: the checkout records what a fill wrote, and a sign-out drops the account", () => {
    const page = read("../app/[locale]/[slug]/booking-page.tsx");
    assert.match(page, /setAcct\(\(state\) => afterFill\(state, signedIn, filled, false\)\);/);
    // Both: the sign-out listener and the unlock.
    assert.equal([...page.matchAll(/if \(clear\) setFlow\(\(f\) => signedOutForm\(f, clear, page\.country\)\);\s*setAcct\(state\);/g)].length, 2);
  });

  test("M5: no account session on the bookings page goes to the account sign-in", () => {
    assert.match(read("../app/[locale]/account/your-bookings.tsx"), /if \(meaning === "signIn"\) router\.replace\("\/account\/sign-in"\);/);
  });

  test("M7: a used sign-up link is forgotten, so coming back does not offer its form again", () => {
    const text = read("../app/[locale]/account/complete/complete-account.tsx");
    assert.match(text, /if \(meaning === "done" && outcome\.data\) \{\s*forgetLink\(TOKEN_KEY, link\);/);
  });

  test("M9: an upper-case slug is not one, for Book again or for the way back", () => {
    const row = { business: { slug: "Salon-Bloom" } };
    assert.equal(bookAgainSlug(row, false), null);
    const items = new Map([[RETURN_KEY, JSON.stringify({ slug: "Salon-Bloom", at: 1 })]]);
    const store = { getItem: (k) => items.get(k) ?? null, setItem: (k, v) => items.set(k, v), removeItem: (k) => items.delete(k) };
    assert.equal(takeReturn(() => store, 2), null);
  });

  test("M10: the account sign-in's challenge uses the action the server verifies", () => {
    // The backend verifies Turnstile with action sign_in for every password guess (app/auth.py require_challenge).
    assert.match(read("../app/[locale]/_ui/account-sign-in.tsx"), /<Turnstile key=\{attempt\} siteKey=\{turnstileSiteKey\} action="sign_in" onToken=\{setToken\} \/>/);
  });

  test("M11: asking for the password again keeps the person's own email filled in", () => {
    assert.match(read("../app/[locale]/account/your-bookings.tsx"), /initialEmail=\{account\.email\}/);
    assert.match(read("../app/[locale]/_ui/account-sign-in.tsx"), /useState\(initialEmail\)/);
  });
});
