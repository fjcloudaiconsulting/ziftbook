// ZIF-102: the business's currency on the settings screen. The owner's ruling: it changes only until
// the business's first booking and before it connects Stripe; saved prices keep their numbers.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

import { changeBody, changeExample, currencyChangeError, currencyChoices, currencyLabel, currencyName, losesPayments } from "../lib/currency.ts";

const option = (code, stripe_settles = false) => ({ code, minor_units: 2, stripe_settles });
const state = {
  currency: "EUR",
  minor_units: 2,
  options: [option("BRL"), option("EUR", true), option("GBP"), option("USD")],
  change: "open",
  locked_by: [],
  prices: 3,
};

describe("currency names", () => {
  test("fence: the name in the reader's language, with the plain symbol after it", () => {
    // Wrong implementation killed: catalog entries, or "US$" for nl and pt readers.
    assert.equal(currencyLabel("EUR", "en"), "Euro (€)");
    assert.equal(currencyLabel("USD", "nl"), `${currencyName("USD", "nl")} ($)`);
    assert.equal(currencyLabel("BRL", "pt"), "Real brasileiro (R$)");
    assert.equal(currencyName("USD", "en"), "US Dollar");
  });
});

describe("the change", () => {
  test("fence: the choices are the other currencies the business can hold, in the API's order", () => {
    assert.deepEqual(currencyChoices(state).map((o) => o.code), ["BRL", "GBP", "USD"]);
  });

  test("fence: the change confirms exactly the saved prices the owner was told about", () => {
    // Wrong implementation killed: no count (the API answers 409 currency_prices_exist), or a
    // boolean confirmation.
    assert.deepEqual(changeBody("USD", 3), { currency: "USD", confirmed_prices: 3 });
    assert.deepEqual(changeBody("USD", 0), { currency: "USD", confirmed_prices: 0 });
  });

  test("fence: the example keeps the number and changes only the currency", () => {
    // Wrong implementation killed: a converted amount; prices keep their minor units.
    assert.deepEqual(changeExample("EUR", "USD", "en"), { from: "€35.00", to: "$35.00" });
    assert.deepEqual(changeExample("EUR", "BRL", "pt"), { from: "€\u00a035,00", to: "R$\u00a035,00" });
  });

  test("fence: a change warns when it leaves the currency Stripe pays out in for the country", () => {
    // Wrong implementation killed: no warning: connecting Stripe is then refused, and after the
    // first booking the currency can no longer change back.
    assert.equal(losesPayments(state, "USD"), true);
    assert.equal(losesPayments({ ...state, currency: "USD" }, "EUR"), false);
    // Already outside it (a country Stripe is not offered in): nothing to lose.
    assert.equal(losesPayments({ ...state, currency: "BRL" }, "USD"), false);
  });
});

describe("currencyChangeError", () => {
  test("fence: each refusal has its own handling, the count first", () => {
    assert.equal(currencyChangeError({ status: 409, code: "currency_prices_exist", total: 4 }), "recount");
    assert.equal(currencyChangeError({ status: 409, code: "currency_locked_bookings" }), "locked");
    assert.equal(currencyChangeError({ status: 409, code: "currency_locked_payments" }), "locked");
    assert.equal(currencyChangeError({ status: 403, code: "owner_only" }), "ownerOnly");
    assert.equal(currencyChangeError({ status: 401 }), "signedOut");
  });

  test("guard: anything else is the generic failure", () => {
    assert.equal(currencyChangeError({ status: 503 }), null);
    assert.equal(currencyChangeError({ status: 409, code: "other" }), null);
  });
});

describe("the copy about payments in a currency", () => {
  test("fence: it states the platform's own limit, never a claim about what Stripe pays out", () => {
    // Wrong implementation killed: "Stripe doesn't pay out in {currency}", which is untrue (Stripe
    // pays out with conversion); the refusal is ziftbook's default (countries.STRIPE_CURRENCIES).
    for (const locale of ["en", "nl", "pt"]) {
      const settings = JSON.parse(readFileSync(new URL(`../messages/${locale}.json`, import.meta.url), "utf8")).Console.settings;
      for (const key of ["paymentsCurrency", "currencyStripeWarning"]) {
        assert.doesNotMatch(settings[key], /pays? out|betaalt|uitbetaal|\bpaga\b/i, `${locale} ${key}`);
        assert.match(settings[key], /\{currency\}/, `${locale} ${key}`);
      }
    }
  });
});
