// ZIF-146: plain logic for a client's own ziftbook account (no business), kept free of React so node --test can
// run it. Nothing here looks anyone up: every answer is about the person signed in on this browser.

import { signInOutcome } from "./account.ts";
import { slugLooksValid } from "./booking-page.ts";
import { defaultDial, regionsWithDial } from "./dial-codes.ts";

/** What an answer to `POST /api/account/session` means for the form. A wrong password is the console's generic
 * answer (`SignIn.invalid`); `account_unavailable` (a platform admin) only follows a correct password. */
export function accountSignInOutcome(
  status: number,
  code: string | undefined,
  hasSiteKey: boolean,
): "ok" | "invalid" | "unavailable" | "challenge" | "try_later" | "error" {
  if (status === 403 && code === "account_unavailable") return "unavailable";
  return signInOutcome(status, code, hasSiteKey);
}

// --- Your bookings (GET /api/account/history) ---

/** The parts of a `HistoryBooking` these read. */
type HistoryRow = { starts_at: string; business: { slug: string } };

/** What an answer to the history means: the list, sign in (no live account session), the password again (the
 * session started more than a day ago: history needs a fresh one), too many reads for now, or a problem. */
export function historyOutcome(status: number, code: string | undefined): "ok" | "signIn" | "reauth" | "tooMany" | "error" {
  if (status === 200) return "ok";
  if (status === 401) return "signIn";
  if (status === 403 && code === "reauthentication_required") return "reauth";
  if (status === 429) return "tooMany";
  return "error";
}

/** Upcoming (not started yet), the next one first; then the past, newest first. */
export function historySections<T extends Pick<HistoryRow, "starts_at">>(bookings: readonly T[], now: Date): { upcoming: T[]; past: T[] } {
  const at = (b: T) => new Date(b.starts_at).getTime();
  const upcoming = bookings.filter((b) => at(b) > now.getTime()).sort((a, b) => at(a) - at(b));
  const past = bookings.filter((b) => at(b) <= now.getTime()).sort((a, b) => at(b) - at(a));
  return { upcoming, past };
}

export type Pill = { label: "waiting" | "confirmed" | "cancelled" | "declined" | "expired" | "noShow"; tone: "ok" | "warn" | "muted" };

/** A row's status pill: "waiting" and "confirmed" for what is ahead, nothing for a past booking that went ahead,
 * and what happened otherwise, muted. */
export function statusPill(status: string, upcoming: boolean): Pill | null {
  if (status === "pending") return { label: "waiting", tone: "warn" };
  if (status === "confirmed" || status === "completed") return upcoming && status === "confirmed" ? { label: "confirmed", tone: "ok" } : null;
  if (status === "cancelled_by_client" || status === "cancelled_by_merchant") return { label: "cancelled", tone: "muted" };
  if (status === "declined") return { label: "declined", tone: "muted" };
  if (status === "expired") return { label: "expired", tone: "muted" };
  if (status === "no_show") return { label: "noShow", tone: "muted" };
  return null;
}

/** "Book again" goes to the business's own page, on past rows only, and only for a slug that is one (so the link
 * can only be `/{locale}/{slug}`). A row opens nothing else: the history carries no booking id and no manage link
 * (security N9). */
export function bookAgainSlug(booking: Pick<HistoryRow, "business">, upcoming: boolean): string | null {
  if (upcoming) return null;
  const slug = booking.business.slug;
  return slugLooksValid(slug) && slug === slug.toLowerCase() ? slug : null;
}

const LANGUAGES = ["en", "nl", "pt"] as const;

/** A service's name in the reader's language, else in the first language it has; `lang` marks text in another
 * language than the page's. */
export function historyServiceName(names: Record<string, string>, locale: string): { text: string; lang: string | null } {
  if (names[locale]) return { text: names[locale], lang: null };
  const other = LANGUAGES.find((l) => names[l]);
  return other ? { text: names[other], lang: other } : { text: "", lang: null };
}

// --- Filling the checkout (security B3.4: only on the person's own click) ---

/** What the offer's click does with `GET /api/account`'s answer: fill in, sign in first (no account session on this
 * browser), or say it failed. */
export function offerNext(status: number): "fill" | "signIn" | "error" {
  if (status === 200) return "fill";
  if (status === 401) return "signIn";
  return "error";
}

/** The account's international phone as the checkout types one: a local number behind its country's code. A code
 * several countries share (+1, +44, +7...) stays the whole international number unless the business's own country
 * is one of them: the picker never shows a guessed country. */
export function splitPhone(phone: string | null, preferred: string): { region: string; local: string } | null {
  if (!phone) return null;
  const digits = phone.replace(/^\+/, "");
  const own = defaultDial(preferred);
  if (own && digits.startsWith(own)) return { region: preferred, local: digits.slice(own.length) };
  for (let length = 1; length <= 3; length++) {
    const regions = regionsWithDial(digits.slice(0, length));
    if (regions.length === 1) return { region: regions[0], local: digits.slice(length) };
    if (regions.length > 1) break;
  }
  return { region: preferred, local: phone };
}

export type Details = { name: string; email: string; phone: string; region: string };
export type Fill = { name: string | null; email: string; phone: string | null; region: string | null };

/** What the account fills in: its name, email and phone (as splitPhone types it). */
export function fillFrom(account: { email: string; name: string | null; phone: string | null }, preferred: string): Fill {
  const phone = splitPhone(account.phone, preferred);
  return { name: account.name, email: account.email, phone: phone?.local ?? null, region: phone?.region ?? null };
}

/** The form after filling: every value the account has replaces what is typed; the rest stays as typed. */
export function applyFill(current: Details, fill: Fill): Details {
  return {
    name: fill.name ?? current.name,
    email: fill.email,
    phone: fill.phone ?? current.phone,
    region: fill.phone !== null && fill.region ? fill.region : current.region,
  };
}

/** An email as the server keeps it (backend/app/passwords.py normalise_email): NFC, no spaces around, lower case. */
function normalEmail(email: string): string {
  return email.trim().toLowerCase().normalize("NFC");
}

/**
 * Whether the email typed differs from the signed-in account's own (ZIF-146 G1): such a booking is a guest's, saved
 * to no account and absent from its bookings, so the field says so. It compares with the person's own address, which
 * this page already holds from their own sign-in: nothing is looked up. Signed out (`account` null) it is always
 * false, whatever is typed; an empty field gets its own error instead.
 */
export function emailNotSaved(account: { email: string } | null, typed: string): boolean {
  if (!account || !typed.trim()) return false;
  return normalEmail(typed) !== normalEmail(account.email);
}
