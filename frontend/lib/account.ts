// Plain logic for the account screens, kept free of React so node --test can run it.

export const RESEND_AFTER_MS = 60_000;

/** Whole seconds until another link may be sent; 0 once it may. */
export function secondsLeft(sentAt: number, now: number): number {
  return Math.max(0, Math.ceil((sentAt + RESEND_AFTER_MS - now) / 1000));
}

export function clock(seconds: number): string {
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

type Place = {
  location: { hash: string; pathname: string; search: string };
  history: { replaceState(data: unknown, unused: string, url: string): void };
  setTimeout(callback: () => void): unknown;
};

/**
 * The token from an emailed link's fragment, or null. The fragment then leaves the address bar, so it can't
 * stay in history, a bookmark or a shared screenshot. It is kept nowhere else: a reload asks for a new link.
 */
export function takeToken(place: Place): string | null {
  const token = place.location.hash.slice(1);
  if (!token) return null;
  const clean = place.location.pathname + place.location.search;
  // Deferred: this runs while the page hydrates, before Next's router wraps history.replaceState. A direct call
  // would leave the router holding the #token address, and a refresh or Back would bring it back.
  place.setTimeout(() => place.history.replaceState(null, "", clean));
  return token;
}

/** The countries a business can be in; the server sets its currency, time zone and language from it.
 * Must match backend/app/countries.py: the API enum only catches extra codes here, not missing ones. */
export const COUNTRIES = ["NL", "PT", "BR", "GB", "US"] as const;
export type Country = (typeof COUNTRIES)[number];

/** Dutch is only spoken in one of them; Portuguese and English each fit two, so those don't guess. */
export function likelyCountry(locale: string): Country | "" {
  return locale === "nl" ? "NL" : "";
}

/** The countries in the reader's alphabetical order of their names. */
export function byName(locale: string, name: (country: Country) => string): Country[] {
  return [...COUNTRIES].sort((a, b) => name(a).localeCompare(name(b), locale));
}

type SignUpRequestErrors = { name?: "nameRequired"; country?: "countryRequired" };

/**
 * Validates the sign-up form and builds the request body, or reports which fields are missing.
 * The server strips spaces too; a blank name would only come back as a vague "invalid request".
 */
export function signUpRequest(
  name: string,
  country: Country | "",
): { errors: SignUpRequestErrors } | { body: { business_name: string; country: Country } } {
  const errors: SignUpRequestErrors = {};
  if (!name.trim()) errors.name = "nameRequired";
  if (!country) errors.country = "countryRequired";
  if (errors.name || errors.country) return { errors };
  return { body: { business_name: name, country } };
}
