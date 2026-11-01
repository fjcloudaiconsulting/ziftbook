// ZIF-102: which language a page opens in, as plain logic (CONTRIBUTING.md, "Which language a person
// sees"). It matches against routing.locales only, never a list of its own (ruling 5).

import { routing } from "../i18n/routing.ts";
import { guardedWrite } from "./console.ts";

export type Locale = (typeof routing.locales)[number];

function known(value: string | null | undefined): Locale | null {
  return value && (routing.locales as readonly string[]).includes(value) ? (value as Locale) : null;
}

// A language the person picked with the language button in this tab that could not be saved to
// their account. It applies to this tab until the page is loaded again ("on this device only for
// now"); a saved pick needs nothing here, the account holds it.
let unsaved: string | null = null;
// Whether the page a pick led to still has to say it could not be saved (once).
let untold = false;

export function recordPick(locale: string, saved: boolean): void {
  unsaved = saved ? null : locale;
  untold = !saved;
}

/** True once, on the page in the language of a pick that could not be saved: the language menu
 * then opens there to say so. */
export function takeUnsavedNotice(locale: string): boolean {
  if (!untold || unsaved !== locale) return false;
  untold = false;
  return true;
}

export function pickedHere(): string | null {
  return unsaved;
}

/** On sign-out: the next person's own language applies. */
export function forgetPick(): void {
  unsaved = null;
  untold = false;
}

/** The first of the browser's languages (navigator.languages, in its order) that we speak, matched
 * on the primary subtag: "pt-PT" and "pt-BR" are both pt. Null when it speaks none of ours. */
export function browserLocale(languages: readonly string[]): Locale | null {
  for (const tag of languages) {
    const found = known(tag.split("-")[0].toLowerCase());
    if (found) return found;
  }
  return null;
}

/**
 * The language this page should be in.
 * - own: the viewer's saved language (SessionOut.locale), null when they chose none.
 * - signedIn: GET /api/session answered for this viewer; a guest is never redirected.
 * - surface: the console falls back to the browser, then the business; a public page to its URL.
 * - picked: a language the person picked by hand in this tab that could not be saved; it applies
 *   here for now, so the page is not sent back to the saved one.
 * Wherever the viewer is known, their saved language wins over the URL and the browser (ruling 4). A
 * guest keeps the URL's: the proxy already gave an unprefixed address the browser's language, else
 * English (ruling 5).
 */
export function resolveLocale(input: {
  own: string | null | undefined;
  signedIn: boolean;
  surface: "console" | "public";
  browser: readonly string[];
  business: string | null | undefined;
  url: Locale;
  picked: string | null;
}): Locale {
  const { own, signedIn, surface, browser, business, url, picked } = input;
  if (!signedIn || known(picked)) return url;
  const saved = known(own);
  if (saved) return saved;
  if (surface === "public") return url;
  return browserLocale(browser) ?? known(business) ?? url;
}

type Identity = { user_id: string; tenant_id: string };

/** A public page's language pick, saved to the signed-in person it was offered to: through the same
 * same-session check as every console write (lib/console.ts guardedWrite), so a sign-in as someone
 * else in another tab is never written. "mismatch": reload, as the console does. */
export async function savePickedLanguage(
  viewer: Identity,
  readSession: () => Promise<{ status: number; data?: Identity }>,
  put: () => Promise<{ status: number }>,
): Promise<"saved" | "failed" | "mismatch" | "signedOut"> {
  const result = await guardedWrite(readSession, viewer, put);
  if (result.kind === "mismatch" || result.kind === "signedOut") return result.kind;
  return result.kind === "sent" && result.outcome.status === 200 ? "saved" : "failed";
}
