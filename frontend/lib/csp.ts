// The pages' Content-Security-Policy (ZB-12). proxy.ts sends it on every page with a fresh nonce; Next puts
// that nonce on its own scripts, and next/script gets it from the layout (app/[locale]/_ui/nonce.tsx).

/** `true` enforces the policy. Anything else sends it report-only: browsers report what it would block,
 * and block nothing. Read once, when the server starts. */
export const CSP_ENFORCE = process.env.ZIF_CSP_ENFORCE === "true";

export const CSP_HEADER = CSP_ENFORCE ? "Content-Security-Policy" : "Content-Security-Policy-Report-Only";

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
export function cspFor(nonce: string, mediaOrigin: string | null, uploadOrigin: string | null = null): string {
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
  ].join("; ");
}
