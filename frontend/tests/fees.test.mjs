// ZIF-80: every fee is shown to the business, ours and Stripe's: the fees block of a booking's
// payment, the plan's fees as percentages, and the link to Stripe's own pricing.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

import { feesOf } from "../lib/calendar.ts";
import { formatMoney } from "../lib/money.ts";
import { percentOf, stripePricingUrl } from "../lib/settings.ts";

const FEES = { stripe_fee_minor: 146, application_fee_minor: 60, net_minor: 3794 };
const PAID = { kind: "pay", status: "paid", amount_minor: 4000, currency: "EUR", refunded_minor: 0, fees: FEES };
const eur = (minor) => formatMoney(minor, "EUR", "en");

describe("feesOf", () => {
  // ZIF-80 fence. Kills: a net shown (or Stripe's fee read as 0) before Stripe reports its fee.
  test("the net is null while the Stripe fee is null", () => {
    const fees = feesOf({ ...PAID, fees: { ...FEES, stripe_fee_minor: null, net_minor: null } }, "en");
    assert.deepEqual(fees, { amount: eur(4000), stripeFee: null, fee: eur(60), net: null, refunded: false });
  });
  test("a known Stripe fee shows every line, the net as the API sent it", () => {
    assert.deepEqual(feesOf(PAID, "en"), { amount: eur(4000), stripeFee: eur(146), fee: eur(60), net: eur(3794), refunded: false });
  });
  // ZIF-80 fence (R34). Kills: a refunded payment's net read as a plain receipt.
  test("a refunded payment marks its net as before the refund", () => {
    assert.equal(feesOf({ ...PAID, refunded_minor: 4000 }, "en").refunded, true);
    const en = JSON.parse(readFileSync(new URL("../messages/en.json", import.meta.url), "utf8"));
    assert.match(en.Console.calendar.feeNetBeforeRefund, /before the refund/);
  });
  // ZIF-80 fence (R35). Kills: a fees block for a worker (the API sends them no fees).
  test("no fees from the API, no fees block", () => {
    assert.equal(feesOf({ ...PAID, fees: null }, "en"), null);
    assert.equal(feesOf({ ...PAID, fees: undefined }, "en"), null);
  });
  test("nothing charged, no fees block", () => {
    assert.equal(feesOf(null, "en"), null);
    assert.equal(feesOf({ ...PAID, kind: "check", status: "checked" }, "en"), null);
    assert.equal(feesOf({ ...PAID, status: "open" }, "en"), null);
  });
});

describe("percentOf", () => {
  // ZIF-80 fence. Kills: a zero fee hidden instead of stated; basis points shown as percent.
  test("states 0% plainly and reads basis points in the reader's format", () => {
    assert.equal(percentOf(0, "en"), "0%");
    assert.equal(percentOf(150, "en"), "1.5%");
    assert.equal(percentOf(150, "nl"), "1,5%");
    assert.equal(percentOf(10000, "en"), "100%");
  });
});

describe("stripePricingUrl", () => {
  test("the business's country page, Stripe's generic page otherwise", () => {
    assert.equal(stripePricingUrl("NL"), "https://stripe.com/nl/pricing");
    assert.equal(stripePricingUrl("GB"), "https://stripe.com/gb/pricing");
    assert.equal(stripePricingUrl(""), "https://stripe.com/pricing");
    assert.equal(stripePricingUrl("../x"), "https://stripe.com/pricing");
    assert.equal(stripePricingUrl("DE"), "https://stripe.com/pricing"); // not a country a business can be in
  });
});
