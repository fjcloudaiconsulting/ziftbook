// The pages' Content-Security-Policy (ZB-12): which scripts, frames, connections and images a page may use.
import assert from "node:assert/strict";
import { afterEach, describe, test } from "node:test";

import { join } from "node:path";

import { topLevelRoutes } from "../../scripts/check-reserved-routes.mjs";
import { cspFor, cspHeaderFor, enforcedFor, newNonce, originOf, OTHER_ROUTES, PERSONAL_ROUTES, personalPage, reportUrlOf, trustedTypesReportOnly } from "../lib/csp.ts";
import { mustReload } from "../lib/personal-pages.ts";

const LOCALE_DIR = join(new URL("..", import.meta.url).pathname, "app", "[locale]");
const LOCALES = ["en", "nl", "pt"];

// directive name -> its sources, as one space-separated string.
function directives(policy) {
  const parsed = {};
  for (const part of policy.split(";")) {
    const [name, ...sources] = part.trim().split(/\s+/);
    if (name) {
      assert.equal(parsed[name], undefined, `${name} appears once`);
      parsed[name] = sources.join(" ");
    }
  }
  return parsed;
}

describe("cspFor", () => {
  const environment = process.env.NODE_ENV;
  afterEach(() => {
    if (environment === undefined) delete process.env.NODE_ENV;
    else process.env.NODE_ENV = environment;
  });

  test("fence: scripts run only from this site with the page's nonce, or when one of those loaded them", () => {
    // Wrong implementation killed: a policy without the nonce, or one that still allows any inline script.
    const policy = cspFor("abc", "https://media.example.com");
    // ZB-12's sources, plus Stripe's documented *.js.stripe.com: only a browser without 'strict-dynamic'
    // reads the hosts at all.
    assert.equal(directives(policy)["script-src"], "'self' 'nonce-abc' 'strict-dynamic' https://js.stripe.com https://*.js.stripe.com https://challenges.cloudflare.com");
    assert.doesNotMatch(directives(policy)["script-src"], /unsafe/);
  });

  test("fence: no plugins, no <base>, forms post only here, and no other site may frame a page", () => {
    const policy = directives(cspFor("abc", "https://media.example.com"));
    assert.equal(policy["default-src"], "'self'");
    assert.equal(policy["object-src"], "'none'");
    assert.equal(policy["base-uri"], "'none'");
    assert.equal(policy["form-action"], "'self'");
    assert.equal(policy["frame-ancestors"], "'none'");
  });

  test("images: this site, data: (the admin's enrolment QR), the media origin, a photo's local preview, and Link's", () => {
    // Wrong implementation killed: an img-src without the media origin, which hides every service photo.
    const policy = cspFor("abc", "https://media.example.com");
    assert.ok(policy.includes("img-src 'self' data: https://media.example.com"));
    // blob: is the photo field's preview of a file not uploaded yet (URL.createObjectURL).
    assert.equal(directives(policy)["img-src"], "'self' data: https://media.example.com blob: https://*.link.com");
  });

  test("images: with no media origin, only this site, data:, blob: and Link's", () => {
    const policy = cspFor("abc", null);
    assert.ok(policy.includes("img-src 'self' data:"));
    assert.equal(directives(policy)["img-src"], "'self' data: blob: https://*.link.com");
    assert.doesNotMatch(policy, /null/);
  });

  test("frames: Stripe's card form and 3-D Secure, Link's sign-in, and the Turnstile challenge", () => {
    // Stripe documents *.js.stripe.com for the frames Stripe.js starts on other js.stripe.com origins, and
    // link.com for Link, which "Pay now"'s dynamic payment methods can offer.
    assert.equal(
      directives(cspFor("abc", null))["frame-src"],
      "https://js.stripe.com https://hooks.stripe.com https://challenges.cloudflare.com https://*.js.stripe.com https://link.com https://*.link.com",
    );
  });

  test("connections: this site, Stripe's API and Link, Turnstile's own reports, and the photo store when one is set", () => {
    // Wrong implementation killed: a connect-src without the store, which blocks every photo upload.
    assert.equal(
      directives(cspFor("abc", null))["connect-src"],
      "'self' https://api.stripe.com https://challenges.cloudflare.com https://link.com https://*.link.com",
    );
    assert.equal(
      directives(cspFor("abc", null, "https://store.example.com"))["connect-src"],
      "'self' https://api.stripe.com https://challenges.cloudflare.com https://link.com https://*.link.com https://store.example.com",
    );
  });

  test("guard: styles fall to default-src 'self', and nothing anywhere allows inline code", () => {
    const policy = cspFor("abc", "https://media.example.com", "https://store.example.com");
    assert.equal(directives(policy)["style-src"], undefined);
    assert.doesNotMatch(policy, /unsafe/);
  });

  test("guard: next dev may evaluate code and add <style> elements (its overlay needs both); a production build may not", () => {
    process.env.NODE_ENV = "development";
    const dev = directives(cspFor("abc", null));
    assert.equal(dev["script-src"], "'self' 'nonce-abc' 'strict-dynamic' https://js.stripe.com https://*.js.stripe.com https://challenges.cloudflare.com 'unsafe-eval'");
    assert.equal(dev["style-src"], "'self' 'unsafe-inline'");
    process.env.NODE_ENV = "production";
    assert.doesNotMatch(cspFor("abc", null), /unsafe|style-src/);
  });
});

describe("newNonce", () => {
  test("fence: 128 random bits, base64, never the same twice", () => {
    const seen = new Set();
    for (let i = 0; i < 100; i++) {
      const nonce = newNonce();
      assert.match(nonce, /^[A-Za-z0-9+/]{22}==$/);
      assert.equal(Buffer.from(nonce, "base64").length, 16);
      seen.add(nonce);
    }
    assert.equal(seen.size, 100);
  });
});

describe("originOf", () => {
  test("a configured URL's origin, with its port and without its path", () => {
    assert.equal(originOf("https://media.example.com/ziftbook-media"), "https://media.example.com");
    assert.equal(originOf("http://localhost:8333/ziftbook-media/"), "http://localhost:8333");
    assert.equal(originOf("https://abc.eu.r2.cloudflarestorage.com"), "https://abc.eu.r2.cloudflarestorage.com");
  });

  test("guard: unset, empty, not a URL, or not http(s): no origin, so nothing extra is allowed", () => {
    for (const value of [undefined, "", "media_url", "mailto:media@example.com", "data:text/plain,x", "ftp://files.example.com"]) {
      assert.equal(originOf(value), null, String(value));
    }
  });
});

// ZIF-146 (security B3): the booking, checkout, manage and account pages share an origin with GET /api/account.
describe("which pages always enforce the policy", () => {
  test("fence: every route folder is classified, so a new page is a personal page or not on purpose", () => {
    // Wrong implementation killed: a new route (say /account/history) that falls to report-only, or a
    // console route that a business slug could shadow being treated as a booking page by accident.
    const unclassified = topLevelRoutes(LOCALE_DIR).filter((r) => !PERSONAL_ROUTES.has(r) && !OTHER_ROUTES.has(r));
    assert.deepEqual(unclassified, []);
  });

  test("the booking, checkout, manage and account pages, in every language, and nothing else", () => {
    for (const path of ["/en/salon-ana", "/nl/salon-ana/book", "/pt/salon-ana/book?step=pay", "/en/booking", "/en/account/sign-in", "/nl/account/complete", "/salon-ana"]) {
      assert.equal(personalPage(path, LOCALES), true, path);
      assert.equal(cspHeaderFor(path, LOCALES), "Content-Security-Policy", path);
    }
    for (const path of ["/", "/en", "/en/sign-in", "/nl/calendar", "/pt/admin/sign-in", "/en/services/new", "/en/terms"]) {
      assert.equal(personalPage(path, LOCALES), false, path);
    }
  });
});


describe("the personal pages' policy: switch, reporting and soft navigation (ZIF-146 code reviews)", () => {
  test("an invite is a personal page: it shows a business's text to someone who may hold an account session", () => {
    assert.equal(personalPage("/en/invite", LOCALES), true);
    assert.equal(cspHeaderFor("/en/invite", LOCALES, false), "Content-Security-Policy");
  });

  test("fence: the emergency switch sends the personal pages' policy report-only again, and nothing else changes", () => {
    // Wrong implementation killed: no switch, so a policy that blocks a payment can only be undone by a redeploy.
    assert.equal(cspHeaderFor("/en/salon-ana/book", LOCALES, true), "Content-Security-Policy-Report-Only");
    assert.equal(cspHeaderFor("/en/salon-ana/book", LOCALES, false), "Content-Security-Policy");
    assert.equal(enforcedFor("/en/sign-in", LOCALES, false), false);
  });

  test("fence: with a report URL every policy names it, in both reporting forms", () => {
    // Wrong implementation killed: an enforced policy that reports nowhere, so a broken checkout goes unseen.
    const url = "https://reports.example.test/csp";
    const policy = directives(cspFor("abc", null, null, url));
    assert.equal(policy["report-uri"], url);
    assert.equal(policy["report-to"], "csp");
    assert.equal(trustedTypesReportOnly(url), `require-trusted-types-for 'script'; report-uri ${url}; report-to csp`);
    // Unset: no reporting directive at all, rather than one pointing nowhere.
    assert.equal(directives(cspFor("abc", null, null, null))["report-uri"], undefined);
    assert.equal(trustedTypesReportOnly(null), "require-trusted-types-for 'script'");
  });

  test("guard: only an https report URL is used", () => {
    assert.equal(reportUrlOf("https://reports.example.test/csp"), "https://reports.example.test/csp");
    for (const value of [undefined, "", "http://reports.example.test", "javascript:alert(1)", "not a url"]) {
      assert.equal(reportUrlOf(value), null, String(value));
    }
  });

  test("fence: a personal page reached by a client-side navigation from a report-only document reloads", () => {
    // Wrong implementation killed: a guard on the current URL, which a client-side navigation has already changed.
    assert.equal(mustReload(true, false, "/en/calendar", LOCALES), true);
    assert.equal(mustReload(true, false, "/en/sign-in", LOCALES), true);
    // Loaded on a personal page: its own policy holds. Policy everywhere, or the switch on: nothing to fix.
    assert.equal(mustReload(true, false, "/en/salon-ana", LOCALES), false);
    assert.equal(mustReload(true, true, "/en/calendar", LOCALES), false);
    assert.equal(mustReload(false, false, "/en/calendar", LOCALES), false);
    assert.equal(mustReload(true, false, null, LOCALES), false);
  });
});
