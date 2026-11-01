// ZIF-146: plain logic for a client's own ziftbook account (no business), kept free of React so node --test can
// run it. Nothing here looks anyone up: every answer is about the person signed in on this browser.

import { signInOutcome } from "./account.ts";
import { phoneValue, slugLooksValid } from "./booking-page.ts";
import { dateLocale } from "./console.ts";
import { defaultDial, regionsWithDial } from "./dial-codes.ts";
import { zoneCity } from "./week.ts";

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

/** When a booking is, at its business's own time, as its booking page, emails and manage page show it: day and
 * time for what is ahead, the day for the past. `city` names the business's zone when it is not the reader's. */
export function historyWhen(
  startsAt: string,
  zone: string,
  readerZone: string,
  locale: string,
  upcoming: boolean,
): { text: string; city: string | null } {
  const at = new Date(startsAt);
  const lang = dateLocale(locale);
  const day = new Intl.DateTimeFormat(lang, { weekday: "short", day: "numeric", month: "short", timeZone: zone }).format(at);
  const time = new Intl.DateTimeFormat(lang, { hour: "2-digit", minute: "2-digit", timeZone: zone }).format(at);
  return { text: upcoming ? `${day}, ${time}` : day, city: zone === readerZone ? null : zoneCity(zone) };
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

/** What the offer's click does with `GET /api/account`'s answer: ask "Continue as {name}?" (an account is already
 * signed in on this browser, maybe someone else's: never a silent fill), sign in first, or say it failed. */
export function offerNext(status: number): "confirm" | "signIn" | "error" {
  if (status === 200) return "confirm";
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

/**
 * The form after "Not you? Sign out": every field still holding what the account filled in empties out (the phone's
 * code goes back to the business's country); anything the account never filled in, or the person changed since,
 * stays as they typed it.
 */
export function afterSignOut(current: Details, written: Fill, businessRegion: string): Details {
  const phoneKept = written.phone === null || current.phone !== written.phone || current.region !== written.region;
  return {
    name: written.name !== null && current.name === written.name ? "" : current.name,
    email: current.email === written.email ? "" : current.email,
    phone: phoneKept ? current.phone : "",
    region: phoneKept ? current.region : businessRegion,
  };
}

// --- Sign-up, email first ---

export type LinkPurpose = "sign_up" | "account_sign_up" | "password_reset";

/** How a link request behaves: its Turnstile action, its "check your inbox" lede and where its "too many" message
 * lives. A client's account sign-up shares business sign-up's action and limits (the backend's own keys), so one
 * inbox gets at most three sign-up emails an hour across both. */
export function linkRequestShape(purpose: LinkPurpose): {
  action: "sign_up" | "password_reset";
  lede: "signUpLede" | "clientLede" | "resetLede";
  tooMany: "SignUp" | "ForgotPassword";
} {
  if (purpose === "password_reset") return { action: "password_reset", lede: "resetLede", tooMany: "ForgotPassword" };
  return { action: "sign_up", lede: purpose === "account_sign_up" ? "clientLede" : "signUpLede", tooMany: "SignUp" };
}

// --- Finishing the account (POST /api/account/sign-up/complete) ---

const PASSWORD_CODES = new Set(["password_too_short", "password_too_long", "password_too_common"]);

/** What the completion's answer means for the screen. */
export function completeOutcome(status: number, code: string | undefined): "done" | "expired" | "exists" | "password" | "phone" | "error" {
  if (status === 201) return "done";
  if (status === 400) return "expired";
  if (status === 409 && code === "account_exists") return "exists";
  if (status === 422 && code && PASSWORD_CODES.has(code)) return "password";
  if (status === 422 && code === "phone_invalid") return "phone";
  return "error";
}

/** The optional phone as the account route reads it: international only (an account has no country), so a local
 * number goes out behind its picked code, as the checkout sends one for another country. Empty is no phone. */
export function accountPhone(region: string, local: string): string | null {
  if (!local.trim()) return null;
  return phoneValue(defaultDial(region), "", local);
}

const LOCALE_REGION: Record<string, string> = { en: "GB", nl: "NL", pt: "BR" };

/** Where the country-code picker starts: the browser's own country when its languages name one we can dial, else
 * the page language's (pt is Brazilian, as the app's copy). */
export function dialRegion(languages: readonly string[], locale: string): string {
  for (const tag of languages) {
    const region = /^[a-z]{2,3}-([A-Z]{2})\b/i.exec(tag)?.[1]?.toUpperCase();
    if (region && defaultDial(region)) return region;
  }
  return LOCALE_REGION[locale] ?? "GB";
}

// --- Back to the booking page after an emailed sign-up (owner ruling 7: this browser only) ---

type StorageLike = Pick<Storage, "getItem" | "setItem" | "removeItem">;

/** Where the way back lives: this browser's own storage, so a link opened in another browser simply has none. */
export const RETURN_KEY = "zif-account-return";
/** As long as the sign-up link works. */
const RETURN_FOR_MS = 24 * 3_600_000;

/** The booking page the done screen's "Create an account" came from: only its slug (never an email or a path), so the
 * way back can only be `/{locale}/{slug}`. `store` is a getter: reading window.localStorage itself can throw. */
export function rememberReturn(store: () => StorageLike, slug: string, now: number): void {
  if (!slugLooksValid(slug) || slug !== slug.toLowerCase()) return;
  try {
    store().setItem(RETURN_KEY, JSON.stringify({ slug, at: now }));
  } catch {
    // Storage refused (private mode, blocked site data): the account still works, with no way back.
  }
}

/** The slug to go back to after the account is created, once: anything else stored there, or older than the link,
 * leads nowhere. */
export function takeReturn(store: () => StorageLike, now: number): string | null {
  try {
    const storage = store();
    const raw = storage.getItem(RETURN_KEY);
    storage.removeItem(RETURN_KEY);
    const value: unknown = JSON.parse(raw ?? "null");
    if (!value || typeof value !== "object") return null;
    const { slug, at } = value as { slug?: unknown; at?: unknown };
    if (typeof slug !== "string" || typeof at !== "number" || now - at > RETURN_FOR_MS) return null;
    return slugLooksValid(slug) && slug === slug.toLowerCase() ? slug : null;
  } catch {
    return null;
  }
}

// --- The checkout's account state: what filled the form in, and when it may change ---

/** The account that filled this page's form in (`written`: what it wrote), and a sign-out's clear still to apply
 * once the form unlocks (`pending`). */
export type AccountState<A> = { account: A | null; written: Fill | null; pending: Fill | null };
export const SIGNED_OUT: AccountState<never> = { account: null, written: null, pending: null };

/** From Send to the answer, and while a sent request waits for its payment, the form's details are the request's:
 * no fill, sign-out clear or offer may change them (PR 6 review, correctness 3). */
export function accountHeld(s: { locked: boolean; busy: boolean; verifying: boolean; working: boolean }): boolean {
  return s.locked || s.busy || s.verifying || s.working;
}

/** After the account filled the form in; while held, nothing changes. */
export function afterFill<A>(state: AccountState<A>, account: A, fill: Fill, held: boolean): AccountState<A> {
  return held ? state : { account, written: fill, pending: null };
}

/** After the account signed out on this browser: free, `clear` is what to empty from the form (afterSignOut); held,
 * nothing is emptied now (afterUnlock applies it). */
export function afterAccountSignOut<A>(state: AccountState<A>, held: boolean): { state: AccountState<A>; clear: Fill | null } {
  // Held, what the account wrote is kept as a clear still to apply: once the form unlocks ("Change time or details"),
  // the next person must not get the previous one's details back, editable.
  return held ? { state: { ...SIGNED_OUT, pending: state.written ?? state.pending }, clear: null } : { state: SIGNED_OUT, clear: state.written };
}

/** When the form unlocks: the clear a sign-out left pending, applied once. */
export function afterUnlock<A>(state: AccountState<A>): { state: AccountState<A>; clear: Fill | null } {
  return { state: { ...state, pending: null }, clear: state.pending };
}

type FormFields = Details & { errors: { name?: string; email?: string; phone?: string; message?: string }; suggest: string | null };

/** The form after a fill, computed from the form as it is now (a functional update): the errors of the fields it
 * wrote go, and the others stay. */
export function filledForm<F extends FormFields>(form: F, fill: Fill): F {
  const errors = {
    ...form.errors,
    email: undefined,
    ...(fill.name !== null ? { name: undefined } : {}),
    ...(fill.phone !== null ? { phone: undefined } : {}),
  };
  return { ...form, ...applyFill(form, fill), errors, suggest: null };
}

/** The form after a sign-out's clear (afterSignOut), from the form as it is now. */
export function signedOutForm<F extends FormFields>(form: F, clear: Fill, businessRegion: string): F {
  return { ...form, ...afterSignOut(form, clear, businessRegion), suggest: null };
}

/** A re-check of the account that filled this page in (security review 6): still the same, ended (signed out, or
 * another person signed in on this browser), or not known (no answer: nothing changes). */
export function recheckOutcome(status: number, data: { email: string } | undefined, account: { email: string }): "same" | "ended" | "unknown" {
  if (status === 401) return "ended";
  if (status !== 200 || !data) return "unknown";
  return normalEmail(data.email) === normalEmail(account.email) ? "same" : "ended";
}

/** Whether the done screen offers "Save your details for next time": only for a booking this page load placed
 * (`placedHere`) with no account signed in on the page. A booking that comes back from a redirect payment is a new
 * page load that may have been placed signed in, and nothing about the person is kept to tell: not offered. */
export function saveDetailsOffer(placedHere: boolean, signedInOnPage: boolean): boolean {
  return placedHere && !signedInOnPage;
}
