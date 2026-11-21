// ZIF-79: the console's plan and billing helpers.
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { bannerFor, billingError, canManage, canSubscribeTo, daysLeft, feeLabel, isStripeUrl, priceLabel, priceNote } from "../lib/billing.ts";

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
    const trial = { ...base, source: "trial", grace_ends_at: "2026-12-01T12:00:00Z", trial_ends_at: "2026-11-25T12:00:00Z" };
    assert.deepEqual(bannerFor(trial, now), { kind: "grace", days: 10 });
    assert.deepEqual(bannerFor({ ...trial, grace_ends_at: null }, now), { kind: "trial", days: 4 });
    assert.deepEqual(bannerFor({ ...base, source: "grant", grant_ends_at: "2027-01-01T00:00:00Z" }, now), { kind: "granted", days: 41 });
    assert.deepEqual(bannerFor({ ...base, source: "grant" }, now), { kind: null, days: 0 });
    assert.deepEqual(bannerFor(base, now), { kind: null, days: 0 });
  });

  test("fence: the trial and grant banners follow where the plan comes from (ZIF-79)", () => {
    // Kills: "your free trial ends" shown to a business that subscribed during its trial, or that was granted a
    // plan while its trial ran; a trial banner with no trial plan set (the plan is the default one).
    const trialDate = "2026-11-25T12:00:00Z";
    assert.equal(bannerFor({ ...base, source: "subscription", trial_ends_at: trialDate }, now).kind, null);
    assert.deepEqual(bannerFor({ ...base, source: "grant", trial_ends_at: trialDate, grant_ends_at: "2027-01-01T00:00:00Z" }, now), { kind: "granted", days: 41 });
    assert.equal(bannerFor({ ...base, source: "default", trial_ends_at: trialDate }, now).kind, null);
    assert.equal(bannerFor({ ...base, source: "trial", grant_ends_at: "2027-01-01T00:00:00Z", trial_ends_at: trialDate }, now).kind, "trial");
  });

  test("fence: an ended trial or grace shows no banner", () => {
    // Kills: "0 days left" banners for dates already past.
    assert.equal(bannerFor({ ...base, source: "trial", trial_ends_at: "2026-11-01T00:00:00Z" }, now).kind, null);
    assert.equal(bannerFor({ ...base, grace_ends_at: "2026-11-01T00:00:00Z" }, now).kind, null);
  });
});

describe("canManage", () => {
  test("fence: a live subscription gets Manage billing, even under a granted plan (ZIF-79)", () => {
    // Kills: a granted business that still pays Stripe left with no way to cancel or see invoices.
    assert.equal(canManage({ source: "grant", period_end: "2026-12-21T00:00:00Z" }, false), true);
    assert.equal(canManage({ source: "subscription", period_end: null }, false), true);
    assert.equal(canManage({ source: "default", period_end: null }, true), true);
    assert.equal(canManage({ source: "grant", period_end: null }, false), false);
    assert.equal(canManage({ source: "trial", period_end: null }, false), false);
  });
});

describe("canSubscribeTo", () => {
  test("fence: the trial's own plan can be bought during the trial (ZIF-79)", () => {
    // Kills: an owner trialling the plan they want unable to subscribe to it until the trial ends.
    assert.equal(canSubscribeTo({ source: "trial", plan_id: "p1" }, "p1"), true);
    assert.equal(canSubscribeTo({ source: "subscription", plan_id: "p1" }, "p1"), false);
    assert.equal(canSubscribeTo({ source: "default", plan_id: "p1" }, "p2"), true);
  });
});

describe("priceLabel, priceNote and feeLabel", () => {
  const option = {
    unit_amount: 1250,
    currency: "EUR",
    interval: "month",
    coupon_id: null,
    percent_off: null,
    amount_off: null,
    coupon_duration: null,
    coupon_duration_in_months: null,
    coupon_valid: null,
  };
  const coupon = { ...option, coupon_id: "c1", coupon_valid: true };
  const text = (label) => label && `${label.key} ${label.values.price} ${label.values.then ?? ""} ${label.values.count ?? ""}`.trim();

  test("the price in the reader's format, per its interval, null when the plan has none", () => {
    assert.deepEqual(priceLabel(option, "en"), { key: "perMonth", values: { price: "€12.50" } });
    assert.match(priceLabel(option, "nl").values.price, /12,50/);
    assert.equal(priceLabel({ ...option, interval: "year" }, "en").key, "perYear");
    assert.equal(priceLabel({ ...option, interval: null }, "en").key, "priceOnly");
    assert.equal(priceLabel({ ...option, unit_amount: null }, "en"), null);
    assert.equal(priceLabel({ ...option, currency: null }, "en"), null);
    // Kills: Stripe's lower-case code ("eur") thrown on by the currency lookup, crashing the Plan tab.
    assert.deepEqual(priceLabel({ ...option, currency: "eur" }, "en"), { key: "perMonth", values: { price: "€12.50" } });
    assert.equal(priceNote(option), null);
  });

  test("fence: a coupon known only by its id never changes the shown price, and is noted", () => {
    // Kills: a guessed discounted price the checkout would not charge; the note lost while the discount is unknown.
    assert.deepEqual(priceLabel({ ...option, coupon_id: "c1" }, "en"), priceLabel(option, "en"));
    assert.equal(priceNote({ ...option, coupon_id: "c1" }), "coupon");
    assert.equal(priceNote({ ...coupon, unit_amount: null, percent_off: 50, coupon_duration: "forever" }), "coupon");
  });

  test("fence: a forever coupon's discount is taken off the shown price (ZIF-79)", () => {
    // Kills: the list price shown for an offer the owner pays less for; a percentage rounded other than Stripe's
    // half-up minor unit; an amount off taking the price below zero; the note shown beside a known discount.
    const forever = { ...coupon, coupon_duration: "forever" };
    assert.equal(text(priceLabel({ ...forever, percent_off: 50 }, "en")), "perMonth €6.25");
    assert.equal(text(priceLabel({ ...forever, percent_off: 12.5 }, "en")), "perMonth €10.94"); // 1250 - 156.25 rounded to 156
    assert.equal(text(priceLabel({ ...forever, amount_off: 500 }, "en")), "perMonth €7.50");
    assert.equal(text(priceLabel({ ...forever, amount_off: 5000 }, "en")), "perMonth €0.00");
    assert.equal(priceNote({ ...forever, percent_off: 50 }), null);
  });

  test("fence: once and repeating coupons say how long the discount lasts, then the list price (ZIF-79)", () => {
    // Kills: a first-month discount shown as the monthly price for good; a repeating coupon's months lost, or
    // counted as years on a yearly price; a yearly price given months that make no whole years.
    const half = { ...coupon, percent_off: 50 };
    assert.equal(text(priceLabel({ ...half, coupon_duration: "once" }, "en")), "onceMonth €6.25 €12.50");
    assert.equal(text(priceLabel({ ...half, interval: "year", coupon_duration: "once" }, "en")), "onceYear €6.25 €12.50");
    const three = { ...half, coupon_duration: "repeating", coupon_duration_in_months: 3 };
    assert.equal(text(priceLabel(three, "en")), "repeatingMonth €6.25 €12.50 3");
    assert.equal(text(priceLabel({ ...three, interval: "year", coupon_duration_in_months: 24 }, "en")), "repeatingYear €6.25 €12.50 2");
    assert.equal(priceNote(three), null);
    const odd = { ...three, interval: "year", coupon_duration_in_months: 3 };
    assert.equal(text(priceLabel(odd, "en")), "perYear €12.50");
    assert.equal(priceNote(odd), "coupon");
  });

  test("fence: a coupon Stripe says is gone shows no discount and says so (ZIF-79)", () => {
    // Kills: a dead coupon's discount still shown (the checkout refuses it); "applied at checkout" promised for a
    // discount that will not be applied.
    const gone = { ...coupon, percent_off: 50, coupon_duration: "forever", coupon_valid: false };
    assert.equal(text(priceLabel(gone, "en")), "perMonth €12.50");
    assert.equal(priceNote(gone), "couponGone");
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
    assert.equal(billingError({ status: 409, code: "coupon_unavailable" }), "couponUnavailable");
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
