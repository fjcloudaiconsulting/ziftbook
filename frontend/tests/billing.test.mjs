// ZIF-79: the console's plan and billing helpers.
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { bannerFor, billingError, daysLeft, discountUnknown, feeLabel, isStripeUrl, priceLabel } from "../lib/billing.ts";

const now = new Date("2026-11-21T12:00:00Z");
const base = { source: "default", trial_ends_at: null, grant_ends_at: null, grace_ends_at: null };

describe("daysLeft", () => {
  test("fence: rounds up, and never goes below zero", () => {
    // Kills: "0 days left" on the last afternoon of a trial, or a negative count once it has ended.
    assert.equal(daysLeft("2026-11-21T12:00:01Z", now), 1);
    assert.equal(daysLeft("2026-11-24T11:00:00Z", now), 3);
    assert.equal(daysLeft("2026-11-20T00:00:00Z", now), 0);
  });
});

describe("bannerFor", () => {
  test("fence: grace beats trial beats a grant, and none shows nothing", () => {
    // Kills: a trial banner hiding the downgrade deadline.
    const all = { ...base, grace_ends_at: "2026-12-01T12:00:00Z", trial_ends_at: "2026-11-25T12:00:00Z", grant_ends_at: "2027-01-01T00:00:00Z" };
    assert.deepEqual(bannerFor(all, now), { kind: "grace", days: 10 });
    assert.deepEqual(bannerFor({ ...all, grace_ends_at: null }, now), { kind: "trial", days: 4 });
    assert.deepEqual(bannerFor({ ...all, grace_ends_at: null, trial_ends_at: null }, now), { kind: "granted", days: 41 });
    assert.deepEqual(bannerFor({ ...base, source: "grant" }, now), { kind: null, days: 0 });
    assert.deepEqual(bannerFor(base, now), { kind: null, days: 0 });
  });

  test("fence: an ended trial or grace shows no banner", () => {
    // Kills: "0 days left" banners for dates already past.
    assert.equal(bannerFor({ ...base, trial_ends_at: "2026-11-01T00:00:00Z" }, now).kind, null);
    assert.equal(bannerFor({ ...base, grace_ends_at: "2026-11-01T00:00:00Z" }, now).kind, null);
  });
});

describe("priceLabel and feeLabel", () => {
  const option = { unit_amount: 1250, currency: "EUR", coupon_id: null, percent_off: null, amount_off: null };
  test("the price in the reader's format, empty when the plan has none", () => {
    assert.match(priceLabel(option, "en"), /12\.50/);
    assert.match(priceLabel(option, "nl"), /12,50/);
    assert.equal(priceLabel({ ...option, unit_amount: null }, "en"), "");
    assert.equal(priceLabel({ ...option, currency: null }, "en"), "");
  });

  test("fence: a coupon known only by its id never changes the shown price, and is noted", () => {
    // Kills: a guessed discounted price the checkout would not charge; the note lost while the discount is unknown.
    assert.equal(priceLabel({ ...option, coupon_id: "c1" }, "en"), priceLabel(option, "en"));
    assert.equal(discountUnknown({ ...option, coupon_id: "c1" }), true);
    assert.equal(discountUnknown(option), false);
  });

  test("fence: the coupon's discount, when the API gives it, is taken off the shown price (ZIF-79)", () => {
    // Kills: the list price shown for an offer the owner pays less for; a percentage rounded other than Stripe's
    // half-up minor unit; an amount off taking the price below zero; the note shown beside a known discount.
    const half = { ...option, coupon_id: "c1", percent_off: 50 };
    assert.match(priceLabel(half, "en"), /6\.25/);
    assert.match(priceLabel({ ...option, percent_off: 12.5 }, "en"), /10\.94/); // 1250 - 156.25 rounded to 156
    assert.match(priceLabel({ ...option, amount_off: 500 }, "en"), /7\.50/);
    assert.match(priceLabel({ ...option, amount_off: 5000 }, "en"), /0\.00/);
    assert.equal(discountUnknown(half), false);
  });

  test("basis points as a percentage, 0% stated", () => {
    assert.equal(feeLabel(0, "en"), "0%");
    assert.equal(feeLabel(150, "en"), "1.5%");
    assert.equal(feeLabel(150, "nl"), "1,5%");
  });
});

describe("billingError", () => {
  test("each refusal maps to its own message", () => {
    assert.equal(billingError({ status: 503, code: "billing_unavailable" }), "unavailable");
    assert.equal(billingError({ status: 409, code: "already_subscribed" }), "alreadySubscribed");
    assert.equal(billingError({ status: 409, code: "plan_granted" }), "granted");
    assert.equal(billingError({ status: 409, code: "not_purchasable" }), "notPurchasable");
    assert.equal(billingError({ status: 409, code: "no_subscription" }), "noSubscription");
    assert.equal(billingError({ status: 404, code: "plan_not_found" }), "notFound");
    assert.equal(billingError({ status: 500 }), "failed");
    assert.equal(billingError({ status: 0 }), "failed");
  });
});

describe("copy", () => {
  test("fence: every plan message exists in en, nl and pt, and every feature the API names has a name", async () => {
    // Kills: a message missing in one language, or a plan_limit that names "undefined" for a feature.
    const { readFileSync } = await import("node:fs");
    const load = (l) => JSON.parse(readFileSync(new URL(`../messages/${l}.json`, import.meta.url), "utf8")).Console;
    const registry = readFileSync(new URL("../../backend/app/entitlements.py", import.meta.url), "utf8");
    const keys = [...registry.matchAll(/^ {4}"(\w+)": Feature\(/gm)].map((m) => m[1]);
    assert.ok(keys.length > 10);
    for (const l of ["en", "nl", "pt"]) {
      const c = load(l);
      for (const k of keys) assert.ok(c.features[k], `${l} features.${k}`);
      for (const ns of ["billing", "planBanner", "planLimit", "features"]) {
        assert.deepEqual(Object.keys(c[ns]).sort(), Object.keys(load("en")[ns]).sort(), `${l} ${ns}`);
      }
    }
  });
});

describe("isStripeUrl", () => {
  test("fence: only https Checkout and portal hosts", () => {
    // Kills: an API answer (or a tampered one) steering the browser to a script, data or look-alike address.
    for (const url of ["https://checkout.stripe.com/c/pay/abc", "https://billing.stripe.com/p/session/x"]) assert.equal(isStripeUrl(url), true, url);
    for (const url of ["javascript:alert(1)", "data:text/html,x", "http://checkout.stripe.com/x", "https://evil.example", "https://checkout.stripe.com.evil.example/x", "not a url", ""])
      assert.equal(isStripeUrl(url), false, url);
  });
});
