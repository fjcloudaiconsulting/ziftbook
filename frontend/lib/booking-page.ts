// Pure logic behind the public booking page (ZIF-56), kept free of React (and of any sibling
// import besides lib/booking-link.ts's addDays) so node --test can run it directly: cancellation
// terms, the business's own policy text, email typo suggestions, the POST body, the answer-to-
// outcome mapping, day-part grouping and the availability window.
import { addDays, slotsByDay } from "./booking-link.ts";

/** Cancellation terms once a slot is picked: free up to `freeUntil`, or already inside that
 * window. The boundary instant itself is still free (spec F1: "at the exact boundary instant,
 * both sides" — this is the side that counts as free). */
export type CancellationState = { kind: "before"; freeUntil: Date } | { kind: "inside"; freeUntil: Date };

export function cancellationState(now: Date, slotStart: Date, freeCancellationHours: number): CancellationState {
  const freeUntil = new Date(slotStart.getTime() - freeCancellationHours * 3_600_000);
  return { kind: now.getTime() <= freeUntil.getTime() ? "before" : "inside", freeUntil };
}

/** The business's own cancellation-policy text, quoted ("In {biz}'s own words") only when the
 * page locale matches the business's own language and the business actually wrote something. */
export function ownPolicyText(text: string | null, pageLocale: string, businessLanguage: string): string | null {
  if (pageLocale !== businessLanguage) return null;
  return text ? text : null;
}

// A handful of common providers a typo-fingered domain is worth flagging against. Not a
// spellchecker: only close misses, never the domain itself. ymail.com/mail.com/email.com are
// their own real providers (Yahoo's alternate domain, GMX's mail.com, and Mail.com's email.com),
// never a "did you mean" target for one another or for gmail.com — listed here so they count as
// an exact match (no suggestion), not a close-but-wrong one.
const KNOWN_DOMAINS = [
  "gmail.com",
  "yahoo.com",
  "hotmail.com",
  "outlook.com",
  "icloud.com",
  "live.com",
  "aol.com",
  "ymail.com",
  "mail.com",
  "email.com",
];

function editDistance(a: string, b: string): number {
  const rows = a.length + 1;
  const cols = b.length + 1;
  const d: number[][] = Array.from({ length: rows }, (_, i) => [i, ...Array(cols - 1).fill(0)]);
  for (let j = 0; j < cols; j++) d[0][j] = j;
  for (let i = 1; i < rows; i++) {
    for (let j = 1; j < cols; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      d[i][j] = Math.min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost);
    }
  }
  return d[rows - 1][cols - 1];
}

/** A non-blocking "did you mean gmail.com?" suggestion: the closest known provider within edit
 * distance 2 of the typed domain, never the domain itself (an exact match suggests nothing), never
 * further than 2 (a genuinely different domain is not a typo). Compares the domain only. */
export function emailSuggestion(email: string): string | null {
  const at = email.lastIndexOf("@");
  if (at < 0) return null;
  const local = email.slice(0, at + 1);
  const domain = email.slice(at + 1).toLowerCase();
  if (!domain || KNOWN_DOMAINS.includes(domain)) return null;
  let best: string | null = null;
  let bestDistance = Infinity;
  for (const provider of KNOWN_DOMAINS) {
    const distance = editDistance(domain, provider);
    if (distance < bestDistance) {
      bestDistance = distance;
      best = provider;
    }
  }
  return best !== null && bestDistance <= 2 ? local + best : null;
}

/** What the client chose in the checkout: have the card checked, or pay now. */
export type PaymentChoice = "check" | "pay";

export type BookingForm = {
  startsAt: string;
  memberId: string | null;
  name: string;
  email: string;
  phone: string;
  locale: string;
  policyVersion: string;
  turnstileToken: string | null;
  consent: boolean;
  message: string | null;
  /** null: the service has no payment step. */
  payment: PaymentChoice | null;
};

export type BookingBody = {
  starts_at: string;
  member_id: string | null;
  name: string;
  email: string;
  phone: string;
  locale: string;
  policy_version: string;
  consents: { marketing_email?: true };
  turnstile_token: string | null;
  message: string | null;
  payment: PaymentChoice | null;
};

/** The POST /api/public/bookings body, exactly the allowed key set. `starts_at` is passed through
 * unchanged (never re-serialised through `Date`, which can turn a "+00:00" offer into "...Z" and
 * no longer match what the availability GET offered). `phone` is required (ZIF-152): the page
 * refuses a blank one before it posts. */
export function bookingBody(form: BookingForm): BookingBody {
  return {
    starts_at: form.startsAt,
    member_id: form.memberId,
    name: form.name,
    email: form.email,
    phone: form.phone.trim(),
    locale: form.locale,
    policy_version: form.policyVersion,
    // Unticked is {} (never false): a false would withdraw a signed-in booker's earlier consent.
    consents: form.consent ? { marketing_email: true } : {},
    turnstile_token: form.turnstileToken,
    message: form.message,
    payment: form.payment,
  };
}

export type AnswerOutcome =
  | { kind: "done" }
  | { kind: "slotTaken" }
  | { kind: "serviceGone" }
  | { kind: "policyChanged" }
  | { kind: "fieldErrors" }
  | { kind: "phoneInvalid" }
  | { kind: "messageInvalid" }
  | { kind: "verifyFailed" }
  | { kind: "unavailable" }
  | { kind: "tooMany" }
  | { kind: "paymentChanged" }
  | { kind: "paymentsUnavailable" }
  | { kind: "nothingBooked" }
  | { kind: "unknown" };

const SLOT_TAKEN_CODES = new Set(["slot_taken", "slot_unavailable"]);
const POLICY_CHANGED_CODES = new Set(["unknown_policy_version", "purpose_not_in_policy_version"]);

/** What the POST answer means for the page (spec's "POST answer mapping" table). `widget`: the page shows Turnstile;
 * without one a Turnstile refusal is the deployment's, and trying again cannot help (ZIF-116). */
export function answerState(answer: { status: number; code?: string }, widget: boolean): AnswerOutcome {
  if (answer.status === 201 || answer.status === 202) return { kind: "done" };
  if (answer.status === 409 && answer.code && SLOT_TAKEN_CODES.has(answer.code)) return { kind: "slotTaken" };
  if (answer.status === 404) return { kind: "serviceGone" };
  if (answer.status === 422 && answer.code && POLICY_CHANGED_CODES.has(answer.code)) return { kind: "policyChanged" };
  if (answer.status === 422 && (answer.code === "payment_required" || answer.code === "payment_not_offered")) return { kind: "paymentChanged" };
  if (answer.status === 422 && answer.code === "phone_invalid") return { kind: "phoneInvalid" };
  if (answer.status === 422 && answer.code === "message_invalid") return { kind: "messageInvalid" };
  if (answer.status === 422) return { kind: "fieldErrors" };
  if (answer.status === 403 && answer.code === "turnstile_failed") return { kind: widget ? "verifyFailed" : "unavailable" };
  if (answer.status === 429) return { kind: "tooMany" };
  if (answer.status === 503 && answer.code === "payments_unavailable") return { kind: "paymentsUnavailable" };
  if (answer.status === 503 && answer.code === "busy") return { kind: "nothingBooked" };
  return { kind: "unknown" };
}

export type DayPart = "morning" | "afternoon" | "evening";

/** Morning/afternoon/evening by the BUSINESS's local hour (< 12, < 17, else), never the viewer's
 * own time zone. */
export function dayPart(instant: string, timeZone: string): DayPart {
  const hour = Number(new Intl.DateTimeFormat("en-GB", { timeZone, hour: "2-digit", hourCycle: "h23" }).format(new Date(instant)));
  if (hour < 12) return "morning";
  if (hour < 17) return "afternoon";
  return "evening";
}

/** A day's free times split into the three groups, each kept in order. */
export function groupByDayPart(slots: string[], timeZone: string): Record<DayPart, string[]> {
  const grouped: Record<DayPart, string[]> = { morning: [], afternoon: [], evening: [] };
  for (const slot of [...slots].sort()) grouped[dayPart(slot, timeZone)].push(slot);
  return grouped;
}

/** Whether "Next week" is disabled: the next week's first day is past today + the booking
 * horizon (both business-local calendar dates, so plain string compare is exact). Exactly at the
 * horizon is still allowed; one day past is not. */
export function nextWeekDisabled(businessLocalToday: string, nextWeekFirstDay: string, horizonDays: number): boolean {
  return nextWeekFirstDay > addDays(businessLocalToday, horizonDays);
}

/** The empty-week scan's availability window: `to` is `from + 13` (14 days inclusive), never
 * `+ 14` (availability.py:424 refuses a 14-day-or-longer span). */
export function scanWindow(from: string): { from: string; to: string } {
  return { from, to: addDays(from, 13) };
}

// Mirrors backend/app/booking_page.py's own check (SLUG regex + isascii + len<=40): a slug that
// fails this is never stored, so it is simply not found — this lets the page loader answer
// notFound() without spending a request on the backend for something it would 404 anyway.
const SLUG = /^[a-z0-9]+(-[a-z0-9]+)*$/;

export function slugLooksValid(slug: string): boolean {
  return slug.length > 0 && slug.length <= 40 && /^[\x00-\x7F]*$/.test(slug) && SLUG.test(slug.toLowerCase());
}

/** The earliest business-local day at or after `from` that has a free slot, out of an already-
 * fetched availability window (never issues a request itself). A 14-day window fetched for the
 * visible week already covers the following week too (spec's scanWindow): when the visible 7 days
 * are empty, this must be checked BEFORE any further network scan — the answer may already be in
 * hand. `from` is inclusive, so days already shown (and confirmed empty by the caller) are simply
 * never in `slots` for a day `< from` that this function would return. */
export function firstFreeDayFrom(slots: string[], from: string, timeZone: string): string | null {
  const days = [...slotsByDay(slots, timeZone).entries()]
    .filter(([day, times]) => times.length > 0 && day >= from)
    .map(([day]) => day)
    .sort();
  return days[0] ?? null;
}

/** The ids free at each slot (ZIF-100): `slot_workers[i]` holds indices into THAT response's own
 * `workers` (sorted by id), never into the page's roster (sorted by name). An index out of range
 * is skipped rather than thrown on, so a skewed answer can't leave the week loading forever. */
export function slotWho(slotWorkers: number[][], workers: { id: string }[]): string[][] {
  return slotWorkers.map((ix) => ix.flatMap((i) => (workers[i] ? [workers[i].id] : [])));
}

/** One person's times out of the "any" window (ZIF-100): the page fetches each window once, for
 * everyone, and filters it locally instead of sending one request per person. */
export function slotsFor(win: { slots: string[]; who: string[][] }, worker: string): string[] {
  return worker === "any" ? win.slots : win.slots.filter((_, i) => win.who[i].includes(worker));
}

/** The people a client may choose at a time: roster order, named only (unnamed people are never
 * offered, decision 6 option A), ids the roster doesn't know dropped. */
export function freeNamed<W extends { id: string; display_name: string | null }>(roster: W[], free: string[]): W[] {
  return roster.filter((w) => w.display_name && free.includes(w.id));
}

/** A step 3 pick survives picking another time only if the new time offers a choice again (two
 * or more named people free) and that person is one of them: with one person free, the booking
 * goes in as no preference (decision 3). */
export function keepPick(pick: string | null, roster: { id: string; display_name: string | null }[], free: string[]): string | null {
  const named = freeNamed(roster, free);
  return pick && named.length > 1 && named.some((w) => w.id === pick) ? pick : null;
}

/** Who the booking goes to (the step 3 pick, else the step 2 filter): their name, `anyone`, or
 * null for a person with no name (shown nowhere, decision 6 option A). */
export function withName(roster: { id: string; display_name: string | null }[], worker: string, pick: string | null, anyone: string): string | null {
  const id = pick ?? (worker === "any" ? null : worker);
  return id === null ? anyone : (roster.find((w) => w.id === id)?.display_name ?? null);
}

/** After a 409 on a picked person: who is still free at that time without them (stay in step 3),
 * or null (the time is gone, or nobody else is free then: today's "choose another time" path).
 * `free` is null when the refreshed window no longer offers the time at all. */
export function afterPickTaken(free: string[] | null, pick: string): string[] | null {
  const rest = (free ?? []).filter((id) => id !== pick);
  return rest.length > 0 ? rest : null;
}

// --- ZIF-157: the checkout page's own state ---

/** The checkout page's path (no locale: the i18n router adds it). Only the service, the person and
 * the time go in the URL, so a refresh restores the pick; whatever else `state` holds (name, email,
 * phone, message) never does. */
export function checkoutUrl(slug: string, state: { serviceId: string; memberId: string | null; at: string | null }): string {
  const q = new URLSearchParams({ service: state.serviceId });
  if (state.memberId) q.set("with", state.memberId);
  if (state.at) q.set("at", state.at);
  return `/${slug}/book?${q}`;
}

/** What the checkout URL says: `with` only if it is one of the service's named people (else
 * anyone, so no stale member_id is posted), `at` only if it parses as an instant. */
export function parseCheckout(
  params: URLSearchParams,
  service: { workers: { id: string; display_name: string | null }[] } | undefined,
): { serviceId: string | null; memberId: string | null; at: string | null } {
  const member = params.get("with");
  const at = params.get("at");
  return {
    serviceId: params.get("service"),
    memberId: member && service?.workers.some((w) => w.id === member && w.display_name) ? member : null,
    at: at && Number.isFinite(Date.parse(at)) ? at : null,
  };
}

/** The first week to show for a picked day: that week's Monday, but never before today (jumpTo's
 * rule), so a pick weeks out comes back with its week on screen. Business-local YYYY-MM-DD. */
export function weekFor(day: string, today: string): string {
  const monday = addDays(day, -((new Date(`${day}T12:00:00Z`).getUTCDay() + 6) % 7));
  return monday < today ? today : monday;
}

/** The time from the URL, only if the window still offers it (a stale one would 409 on Send). */
export function pickFromUrl(at: string | null, slots: string[]): string | null {
  return at !== null && slots.includes(at) ? at : null;
}

/** The message as posted: CRLF and CR become LF first (CR is a control character), then tabs become
 * spaces, then every other control/format character (except LF) and U+2028/2029 goes; blank is
 * null. The backend refuses what this removes (422 message_invalid). */
export function cleanMessage(text: string): string | null {
  const cleaned = text
    .replace(/\r\n?/g, "\n")
    .replace(/\t/g, " ")
    .replace(/[\p{C}\u2028\u2029]/gu, (c) => (c === "\n" ? c : ""))
    .trim();
  return cleaned === "" ? null : cleaned;
}

/** The phone as posted (the backend reads it in the business country): a leading "+" is kept as
 * typed, "00" becomes "+", the business's own code leaves the number as typed, else the chosen
 * code is put in front. */
export function phoneValue(dial: string, businessDial: string, local: string): string {
  const typed = local.trim();
  if (typed.startsWith("+")) return typed;
  if (typed.startsWith("00")) return `+${typed.slice(2)}`;
  return dial === businessDial ? typed : `+${dial}${typed}`;
}

// --- ZIF-158: the payment step ---

/** Stripe's locale for each page language (the app's pt is Brazilian, as TURNSTILE_LANG). */
export const STRIPE_LOCALE: Record<string, string> = { en: "en", nl: "nl", pt: "pt-BR" };

export type ElementsOptions =
  | { mode: "payment"; amount: number; currency: string; captureMethod: "manual" | "automatic"; paymentMethodTypes?: string[] }
  | { mode: "setup"; currency: string; paymentMethodTypes: string[] };

/** The deferred Payment Element's options, which must equal the server's intent (Stripe refuses the
 * confirm otherwise): a check is card only (held, or a setup where the card network allows no hold);
 * paying in advance offers every method the account has. */
export function elementsOptions(
  choice: PaymentChoice,
  checkWith: "authorisation" | "setup",
  price: { amount_minor: number; currency: string },
): ElementsOptions {
  const currency = price.currency.toLowerCase();
  if (choice === "pay") return { mode: "payment", amount: price.amount_minor, currency, captureMethod: "automatic" };
  if (checkWith === "setup") return { mode: "setup", currency, paymentMethodTypes: ["card"] };
  return { mode: "payment", amount: price.amount_minor, currency, captureMethod: "manual", paymentMethodTypes: ["card"] };
}

const KEPT = ["service", "with", "at", "booking"];

/** The checkout's own query, minus whatever Stripe appended on the way back (the client secret must
 * never stay in history or logs). */
export function returnParams(search: string): URLSearchParams {
  const from = new URLSearchParams(search);
  const kept = new URLSearchParams();
  for (const key of KEPT) {
    const value = from.get(key);
    if (value !== null) kept.set(key, value);
  }
  return kept;
}

/** Extra lines for the summary's terms block: the kept card fee, only while Pay now is chosen and
 * the platform really keeps it (R6: false at launch). Message keys of BookingPage. */
export function termsLines(choice: PaymentChoice | null, keepsCardFee: boolean): "termsPaymentFee"[] {
  return choice === "pay" && keepsCardFee ? ["termsPaymentFee"] : [];
}

/** The Send button's message key: it states the obligation to pay when paying now (EU CRD art. 8(2)). */
export function sendLabel(choice: PaymentChoice | null, autoConfirm: boolean): "payButtonBook" | "payButtonRequest" | "book" | "request" {
  if (choice === "pay") return autoConfirm ? "payButtonBook" : "payButtonRequest";
  return autoConfirm ? "book" : "request";
}

export type PayView =
  | { kind: "done" }
  | { kind: "resume"; failed: boolean }
  | { kind: "processing" }
  | { kind: "expired" }
  | { kind: "expiredPaid" }
  | { kind: "expiredProcessing" }
  | { kind: "stale" };

/** What the page shows for a booking and its payment (SyncOut): never "booked" for anything but
 * pending or confirmed, and never "choose a time again" while money may still land. */
export function doneState(sync: { status: string; payment: { state: string; failed: boolean } }): PayView {
  const { status, payment } = sync;
  if (status === "pending" || status === "confirmed") return { kind: "done" };
  if (status === "awaiting_payment") {
    if (payment.state === "open") return { kind: "resume", failed: payment.failed };
    // checked or paid while the booking has not moved yet: the webhook is about to.
    return payment.state === "canceled" ? { kind: "stale" } : { kind: "processing" };
  }
  if (status === "expired") {
    return payment.state === "processing" ? { kind: "expiredProcessing" } : payment.state === "paid" ? { kind: "expiredPaid" } : { kind: "expired" };
  }
  return { kind: "stale" };
}

/** A refresh, a Back from a bank page or Stripe's return carries `booking`: that always resumes,
 * whatever `at` says. Without it, an id stored for THIS service is checked once; else a plain checkout. */
export function entryMode(search: URLSearchParams, stored: { bookingId: string; serviceId: string } | null): "resume" | "stored" | "checkout" {
  if (search.get("booking")) return "resume";
  return stored !== null && stored.serviceId === search.get("service") ? "stored" : "checkout";
}

/** What an id stored by an earlier visit turns out to be: still waiting for payment (offer to resume),
 * already pending or confirmed (show it), or nothing to act on. */
export function storedNext(sync: { status: string }): "offerResume" | "done" | "checkout" {
  if (sync.status === "awaiting_payment") return "offerResume";
  return sync.status === "pending" || sync.status === "confirmed" ? "done" : "checkout";
}
