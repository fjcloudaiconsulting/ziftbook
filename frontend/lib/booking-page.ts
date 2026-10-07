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

export type BookingForm = {
  startsAt: string;
  memberId: string | null;
  name: string;
  email: string;
  phone: string;
  locale: string;
  policyVersion: string;
  turnstileToken: string | null;
};

export type BookingBody = {
  starts_at: string;
  member_id: string | null;
  name: string;
  email: string;
  phone?: string;
  locale: string;
  policy_version: string;
  consents: Record<string, never>;
  turnstile_token: string | null;
};

/** The POST /api/public/bookings body, exactly the allowed key set. `starts_at` is passed through
 * unchanged (never re-serialised through `Date`, which can turn a "+00:00" offer into "...Z" and
 * no longer match what the availability GET offered). `phone` is omitted, never sent as `""`, when
 * blank after trim (the backend's strict body 422s on an empty string). */
export function bookingBody(form: BookingForm): BookingBody {
  const phone = form.phone.trim();
  return {
    starts_at: form.startsAt,
    member_id: form.memberId,
    name: form.name,
    email: form.email,
    ...(phone ? { phone } : {}),
    locale: form.locale,
    policy_version: form.policyVersion,
    consents: {},
    turnstile_token: form.turnstileToken,
  };
}

export type AnswerOutcome =
  | { kind: "done" }
  | { kind: "slotTaken" }
  | { kind: "serviceGone" }
  | { kind: "policyChanged" }
  | { kind: "fieldErrors" }
  | { kind: "verifyFailed" }
  | { kind: "tooMany" }
  | { kind: "nothingBooked" }
  | { kind: "unknown" };

const SLOT_TAKEN_CODES = new Set(["slot_taken", "slot_unavailable"]);
const POLICY_CHANGED_CODES = new Set(["unknown_policy_version", "purpose_not_in_policy_version"]);

/** What the POST answer means for the page (spec's "POST answer mapping" table). */
export function answerState(answer: { status: number; code?: string }): AnswerOutcome {
  if (answer.status === 201) return { kind: "done" };
  if (answer.status === 409 && answer.code && SLOT_TAKEN_CODES.has(answer.code)) return { kind: "slotTaken" };
  if (answer.status === 404) return { kind: "serviceGone" };
  if (answer.status === 422 && answer.code && POLICY_CHANGED_CODES.has(answer.code)) return { kind: "policyChanged" };
  if (answer.status === 422) return { kind: "fieldErrors" };
  if (answer.status === 403 && answer.code === "turnstile_failed") return { kind: "verifyFailed" };
  if (answer.status === 429) return { kind: "tooMany" };
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
