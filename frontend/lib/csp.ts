// The pages' Content-Security-Policy (ZB-12). proxy.ts sends it on every page with a fresh nonce; Next puts
// that nonce on its own scripts, and next/script gets it from the layout (app/[locale]/_ui/nonce.tsx).

/** `true` enforces the policy. Anything else sends it report-only: browsers report what it would block,
 * and block nothing. Read once, when the server starts. */
export const CSP_ENFORCE = process.env.ZIF_CSP_ENFORCE === "true";

export const CSP_HEADER = CSP_ENFORCE ? "Content-Security-Policy" : "Content-Security-Policy-Report-Only";

/** The name the policies' `report-to` uses for the `Reporting-Endpoints` entry proxy.ts sends. */
export const REPORT_GROUP = "csp";

// The API's report route on an https host, with no credentials, query or fragment. A `;` or `,` would add a
// directive or split the header into two policies, and a `"` would end Reporting-Endpoints' quoted value. Pinning
// the path keeps raw reports (a checkout's carries Stripe's client secret in its URL) away from any collector but
// the API, which logs only route templates and categories. The host itself can't be pinned: the web server is not
// told its public origin.
const REPORT_URL = /^https:\/\/[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*(?::([1-9]\d{0,4}))?\/api\/csp-reports$/;

/** The value, when it is exactly the API's report route on an https host, written as a browser writes it. */
export function reportUrlOf(value: string | undefined): string | null {
  if (!value) return null;
  try {
    // Equal to its own parse: no whitespace, backslash, upper case, default port or empty `?`/`#` slips through
    // the parser's normalising.
    if (new URL(value).href !== value) return null;
  } catch {
    return null;
  }
  const match = REPORT_URL.exec(value);
  return match && (match[1] === undefined || Number(match[1]) <= 65535) ? value : null;
}

/** Where browsers send violation reports (ZIF-236): this deployment's own `https://<host>/api/csp-reports`, or null
 * when unset or refused (reportUrlOf), and then no policy names one. Read once, when the server starts. */
export const CSP_REPORT_URL = reportUrlOf(process.env.ZIF_CSP_REPORT_URL);

// A Host header as a browser sends it: a DNS name or IPv4 address, with an optional port. Nothing else is compared.
const HOST = /^[A-Za-z0-9.-]+(?::\d{1,5})?$/;

/** The report URL for a page served on `host` (the request's Host header), or null when its host is another:
 * a typo'd or another environment's URL would otherwise ship every visitor's raw reports there cross-origin
 * (report-uri needs no preflight). A forged Host changes only the forger's own page. */
export function reportUrlFor(host: string | null | undefined, reportUrl: string | null = CSP_REPORT_URL): string | null {
  if (!reportUrl || !host || !HOST.test(host)) return null;
  try {
    return new URL(`https://${host}`).host === new URL(reportUrl).host ? reportUrl : null;
  } catch {
    return null;
  }
}

/** The reporting directives for a policy: both forms, as browsers support one or the other. */
export function reportingDirectives(reportUrl: string | null): string[] {
  return reportUrl ? [`report-uri ${reportUrl}`, `report-to ${REPORT_GROUP}`] : [];
}

/** The web server's startup fields for the policy, never the report URL; `refused` when ZIF_CSP_REPORT_URL is set
 * but not used, so reports would be silently off. */
export function cspStartupFields(
  raw: string | undefined = process.env.ZIF_CSP_REPORT_URL,
  enforce: boolean = CSP_ENFORCE,
): { fields: { csp: "enforce" | "report-only"; csp_reports: "on" | "off" }; refused: boolean } {
  const url = reportUrlOf(raw);
  return {
    fields: { csp: enforce ? "enforce" : "report-only", csp_reports: url ? "on" : "off" },
    refused: Boolean(raw) && url === null,
  };
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
 * `uploadOrigin` is the store the browser uploads a photo to; `reportUrl` (CSP_REPORT_URL) where violations go.
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
