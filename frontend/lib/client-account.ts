// ZIF-146: plain logic for a client's own ziftbook account (no business), kept free of React so node --test can
// run it. Nothing here looks anyone up: every answer is about the person signed in on this browser.

import { signInOutcome } from "./account.ts";
import { slugLooksValid } from "./booking-page.ts";

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
