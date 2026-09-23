// A service's price: formatting for display, parsing the price field back to minor units, and the
// prefill/sign helpers the form needs. Always 2 minor units (services.py:53, countries.py:16-17).
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { currencySign, formatMoney, parseMoney, priceText, signFirst } from "../lib/money.ts";

describe("formatMoney", () => {
  test("3500 EUR: nl, pt and en all use a non-breaking space before the sign's amount", () => {
    assert.equal(formatMoney(3500, "EUR", "nl"), "€ 35,00");
    assert.equal(formatMoney(3500, "EUR", "pt"), "€ 35,00");
    assert.equal(formatMoney(3500, "EUR", "en"), "€35.00");
  });

  test("3500 BRL pt, and 3500 USD nl: the currency's own sign, not a hardcoded €", () => {
    assert.equal(formatMoney(3500, "BRL", "pt"), "R$ 35,00");
    assert.equal(formatMoney(3500, "USD", "nl"), "US$ 35,00");
  });

  test("CHF en falls back to the currency code (no sign)", () => {
    assert.equal(formatMoney(3500, "CHF", "en"), "CHF 35.00");
  });
});

describe("parseMoney", () => {
  test('"12,5" is one comma decimal separator: 1250', () => {
    assert.equal(parseMoney("12,5"), 1250);
  });

  test('"0.29" rounds to the cent, never truncates: 29', () => {
    assert.equal(parseMoney("0.29"), 29);
  });

  test('"35,999" has three decimals: refused, not rounded', () => {
    assert.equal(parseMoney("35,999"), null);
  });

  test('"35" (no decimals) is 3500', () => {
    assert.equal(parseMoney("35"), 3500);
  });

  test("grouping, negative and over-limit values are refused", () => {
    assert.equal(parseMoney("1.000"), null);
    assert.equal(parseMoney("-1"), null);
    assert.equal(parseMoney("10000,01"), null);
  });
});

describe("priceText", () => {
  test("round-trips through parseMoney with no grouping, even above 1000", () => {
    assert.equal(parseMoney(priceText(123456, "nl")), 123456);
  });
});

describe("currencySign / signFirst", () => {
  test("EUR nl and en: this app's three locales all put the sign before the amount", () => {
    assert.equal(currencySign(3500, "EUR", "nl"), "€");
    assert.equal(signFirst("EUR", "nl"), true);
    assert.equal(currencySign(3500, "EUR", "en"), "€");
    assert.equal(signFirst("EUR", "en"), true);
  });

  test("CHF en: the code stands in for the sign, and still comes first", () => {
    assert.equal(currencySign(3500, "CHF", "en"), "CHF");
    assert.equal(signFirst("CHF", "en"), true);
  });

  // No fence for a trailing sign: every combination this app can actually reach — its three
  // locales (en, nl, pt) crossed with the currencies a business can hold (EUR, BRL, GBP, USD;
  // countries.py:16-17) — puts the sign first (checked on node 24). A test asserting the opposite
  // for some other locale/currency pair would never fail on a real regression, so there is
  // deliberately none here rather than a fence that can't fail.
  test("every locale x currency this app can reach puts the sign first", () => {
    for (const locale of ["en", "nl", "pt"]) {
      for (const currency of ["EUR", "BRL", "GBP", "USD"]) {
        assert.equal(signFirst(currency, locale), true, `${locale} ${currency}`);
      }
    }
  });
});
