// The pages' Content-Security-Policy (ZB-12). proxy.ts sends it on every page with a fresh nonce; Next puts
// that nonce on its own scripts, and next/script gets it from the layout (app/[locale]/_ui/nonce.tsx).

/** `true` enforces the policy on every page. Anything else sends it report-only (browsers report what it
 * would block, and block nothing), except on the pages personalPage() names, which always enforce it.
 * Read once, when the server starts. */
export const CSP_ENFORCE = process.env.ZIF_CSP_ENFORCE === "true";

export const CSP_HEADER = CSP_ENFORCE ? "Content-Security-Policy" : "Content-Security-Policy-Report-Only";

/** Emergency switch (ZIF-146): `true` sends the personal pages' policy report-only again, as every other page's,
 * without a build, should the enforced policy ever block a payment or the challenge in production. Anything else
 * (the default) enforces it there. Read once, when the server starts. */
export const CSP_PERSONAL_REPORT_ONLY = process.env.ZIF_CSP_PERSONAL_REPORT_ONLY === "true";

/** Where browsers send violation reports (ZIF-236 provides the endpoint): an https URL, or null when unset or not
 * https, and then no policy names one. Read once, when the server starts. */
export const CSP_REPORT_URL = reportUrlOf(process.env.ZIF_CSP_REPORT_URL);

/** The name the policies' `report-to` uses for the `Reporting-Endpoints` entry proxy.ts sends. */
export const REPORT_GROUP = "csp";

export function reportUrlOf(value: string | undefined): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" ? url.href : null;
  } catch {
    return null;
  }
}

/** The reporting directives for a policy: both forms, as browsers support one or the other. */
export function reportingDirectives(reportUrl: string | null): string[] {
  return reportUrl ? [`report-uri ${reportUrl}`, `report-to ${REPORT_GROUP}`] : [];
}

/** The top-level routes under app/[locale] that show or fetch a person's own data: the account pages
 * (ZIF-146) and a booking's manage page (`booking`). */
export const PERSONAL_ROUTES: ReadonlySet<string> = new Set(["account", "booking"]);

/** Every other top-level route under app/[locale]: the console, the admin, sign-in and the legal pages.
 * Any first segment in neither set is a business's slug: its booking page, its booking and checkout
 * steps. tests/csp.test.mjs fails for a route folder in neither set, so a new route is classified on
 * purpose. */
export const OTHER_ROUTES: ReadonlySet<string> = new Set([
  "admin",
  "calendar",
  "clients",
  "forgot-password",
  "invite",
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

/** Whether this page's policy is enforced: every personal page, unless the emergency switch is on, and every page
 * when ZIF_CSP_ENFORCE is. */
export function enforcedFor(
  pathname: string,
  locales: readonly string[],
  personalReportOnly: boolean = CSP_PERSONAL_REPORT_ONLY,
): boolean {
  return CSP_ENFORCE || (personalPage(pathname, locales) && !personalReportOnly);
}

/** The policy header a page gets (enforcedFor). */
export function cspHeaderFor(
  pathname: string,
  locales: readonly string[],
  personalReportOnly: boolean = CSP_PERSONAL_REPORT_ONLY,
): string {
  return enforcedFor(pathname, locales, personalReportOnly) ? "Content-Security-Policy" : "Content-Security-Policy-Report-Only";
}

/** Sent report-only beside the enforced policy on personal pages: browsers report every DOM sink that
 * takes a plain string (innerHTML and kin), so the pages can be made Trusted Types clean before it is
 * enforced there. */
export function trustedTypesReportOnly(reportUrl: string | null = CSP_REPORT_URL): string {
  return ["require-trusted-types-for 'script'", ...reportingDirectives(reportUrl)].join("; ");
}

/** 128 random bits, base64: a page's nonce, never reused. */
export function newNonce(): string {
  return btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(16))));
}

/** A configured URL's origin (`https://media.example.com`), or null when it is unset or not an http(s) URL. */
export function originOf(value: string | undefined): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:" ? url.origin : null;
  } catch {
    return null;
  }
}

// Stripe's Link: its sign-in frames and their calls (Stripe's CSP guide, "Link").
const LINK = "https://link.com https://*.link.com";

/**
 * The policy for one page. Scripts run only with the page's nonce, or when a script that had it loaded them
 * ('strict-dynamic': Next's chunks, Stripe.js, Turnstile's api.js). `mediaOrigin` serves the service photos;
 * `uploadOrigin` is the store the browser uploads a photo to.
 */
export function cspFor(
  nonce: string,
  mediaOrigin: string | null,
  uploadOrigin: string | null = null,
  reportUrl: string | null = CSP_REPORT_URL,
): string {
  // `next dev` only: React's development build evaluates code, and Next's dev overlay adds <style> elements.
  const dev = process.env.NODE_ENV === "development";
  return [
    // Also styles and fonts: Next links its CSS and fonts from this site, React applies `style` props from
    // script (no style attribute is in a page's HTML), and Stripe.js and Turnstile style their elements the same way.
    "default-src 'self'",
    // The hosts only matter to a browser without 'strict-dynamic', which falls back to them; *.js.stripe.com
    // is on Stripe's documented Stripe.js list.
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic' https://js.stripe.com https://*.js.stripe.com https://challenges.cloudflare.com${dev ? " 'unsafe-eval'" : ""}`,
    ...(dev ? ["style-src 'self' 'unsafe-inline'"] : []),
    // *.js.stripe.com: Stripe.js starts some of its frames on other js.stripe.com origins (Stripe's documented
    // list). link.com: Stripe's Link, which the Payment Element can offer on "Pay now", whose payment methods
    // are dynamic (lib/booking-page.ts elementsOptions, the API's automatic_payment_methods); Stripe's CSP
    // guide lists these frame, connect and image sources for it.
    `frame-src https://js.stripe.com https://hooks.stripe.com https://challenges.cloudflare.com https://*.js.stripe.com ${LINK}`,
    // challenges.cloudflare.com: the Turnstile script's own reports, sent from the page.
    `connect-src 'self' https://api.stripe.com https://challenges.cloudflare.com ${LINK}${uploadOrigin ? ` ${uploadOrigin}` : ""}`,
    // data: is the admin's enrolment QR code; blob: a photo's preview before it is uploaded.
    `img-src 'self' data:${mediaOrigin ? ` ${mediaOrigin}` : ""} blob: https://*.link.com`,
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    ...reportingDirectives(reportUrl),
  ].join("; ");
}
