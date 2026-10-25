// ZIF-146: which pages always enforce their Content-Security-Policy (lib/csp.ts), and the client-side check that
// keeps a soft navigation from rendering one of them inside a document whose policy only reports. Pure: no
// environment, so client components import it too.

/** The top-level routes under app/[locale] that show or fetch a person's own data, or render a business's text to
 * someone who may hold an account session: the account pages (ZIF-146), a booking's manage page (`booking`) and
 * an invite (`invite`, the business's name, to a person who may already have an account). */
export const PERSONAL_ROUTES: ReadonlySet<string> = new Set(["account", "booking", "invite"]);

/** Every other top-level route under app/[locale]: the console, the admin, sign-in and the legal pages.
 * Any first segment in neither set is a business's slug: its booking page, its booking and checkout
 * steps. tests/csp.test.mjs fails for a route folder in neither set, so a new route is classified on
 * purpose. */
export const OTHER_ROUTES: ReadonlySet<string> = new Set([
  "admin",
  "calendar",
  "clients",
  "forgot-password",
  "my-hours",
  "opening-hours",
  "privacy",
  "reset-password",
  "services",
  "settings",
  "setup",
  "sign-in",
  "sign-up",
  "team",
  "terms",
]);

/**
 * Whether a page always gets the enforced policy, whatever ZIF_CSP_ENFORCE says (ZIF-146): a business's
 * booking pages (`/{locale}/{slug}`, `/{locale}/{slug}/book`, its checkout), the manage page and the
 * account pages. They render merchant-controlled text on the origin that answers `GET /api/account` with a
 * signed-in person's details, so a script that got onto them must not run. Everywhere else stays report-only
 * until ZIF-224 enforces it everywhere.
 */
export function personalPage(pathname: string, locales: readonly string[]): boolean {
  const segments = pathname.split("/").filter(Boolean);
  if (segments.length > 0 && locales.includes(segments[0])) segments.shift();
  const first = segments[0];
  if (first === undefined) return false; // the home page
  if (PERSONAL_ROUTES.has(first)) return true;
  return !OTHER_ROUTES.has(first);
}

/**
 * Whether a personal page must reload to get its own policy (security code review N-9). A policy belongs to the
 * document: a client-side navigation from a page that only reports (the console, sign-in) into a booking or
 * account page would render it under the report-only policy. `enforced` is what the server decided for this page;
 * `everywhere` is ZIF_CSP_ENFORCE; `documentPath` is the path the document was loaded at (the navigation entry,
 * which a client-side navigation never changes).
 */
export function mustReload(
  enforced: boolean,
  everywhere: boolean,
  documentPath: string | null,
  locales: readonly string[],
): boolean {
  if (!enforced || everywhere || documentPath === null) return false;
  return !personalPage(documentPath, locales);
}
