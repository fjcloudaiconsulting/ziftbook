// A service's price: formatting for display, parsing the price field back to minor units, and the
// prefill/sign helpers the form needs. The currency's own minor units (ZIF-102): the four a business
// can hold (backend/app/currencies.py) all have 2; JPY (0) and BHD (3) are the fence cases.
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { currencySign, formatMoney, minorUnits, parseMoney, priceText, signFirst } from "../lib/money.ts";

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
    assert.equal(parseMoney("12,5", "EUR"), 1250);
  });

  test('"0.29" rounds to the cent, never truncates: 29', () => {
    assert.equal(parseMoney("0.29", "EUR"), 29);
  });

  test('"35,999" has three decimals: refused, not rounded', () => {
    assert.equal(parseMoney("35,999", "EUR"), null);
  });

  test('"35" (no decimals) is 3500', () => {
    assert.equal(parseMoney("35", "EUR"), 3500);
  });

  test("grouping, negative and over-limit values are refused", () => {
    assert.equal(parseMoney("1.000", "EUR"), null);
    assert.equal(parseMoney("-1", "EUR"), null);
    assert.equal(parseMoney("10000,01", "EUR"), null);
  });
});

describe("priceText", () => {
  test("round-trips through parseMoney with no grouping, even above 1000", () => {
    assert.equal(parseMoney(priceText(123456, "EUR", "nl"), "EUR"), 123456);
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

// ZIF-102: minor units come from the currency, never an assumed 2.
describe("minor units", () => {
  test("guard: the four currencies a business can hold have 2; JPY 0 and BHD 3, as ISO 4217", () => {
    for (const currency of ["BRL", "EUR", "GBP", "USD"]) assert.equal(minorUnits(currency), 2, currency);
    assert.equal(minorUnits("JPY"), 0);
    assert.equal(minorUnits("BHD"), 3);
  });

  // Fence. Wrong impl: formatMoney's `minor / 100`.
  test("formatMoney scales by the currency's minor units", () => {
    assert.equal(formatMoney(3500, "JPY", "en"), "¥3,500");
    assert.equal(formatMoney(1234500, "BHD", "en"), "BHD\u00a01,234.500");
    assert.equal(formatMoney(5, "BHD", "pt"), "BHD\u00a00,005");
  });

  // Fence. Wrong impl: parseMoney's fixed `\d{1,2}` decimals and `* 100`.
  test("parseMoney reads as many decimals as the currency has, and no more", () => {
    assert.equal(parseMoney("35", "JPY"), 35);
    assert.equal(parseMoney("35,5", "JPY"), null);
    assert.equal(parseMoney("1,234", "BHD"), 1234);
    assert.equal(parseMoney("1,2345", "BHD"), null);
    assert.equal(parseMoney("12,5", "EUR"), 1250);
  });

  // Fence. Wrong impl: a bound written in major units (10000.00), which lets a JPY price of
  // 1 000 001 or a BHD one of 1000.001 through.
  test("parseMoney refuses more than 1 000 000 minor units in every currency", () => {
    assert.equal(parseMoney("1000000", "JPY"), 1_000_000);
    assert.equal(parseMoney("1000001", "JPY"), null);
    assert.equal(parseMoney("1000,000", "BHD"), 1_000_000);
    assert.equal(parseMoney("1000,001", "BHD"), null);
  });

  // Fence. Wrong impl: priceText with 2 fraction digits, which parseMoney then refuses (JPY) or
  // misreads (BHD).
  test("priceText round-trips through parseMoney for every currency and language", () => {
    for (const currency of ["EUR", "JPY", "BHD"]) {
      for (const locale of ["en", "nl", "pt"]) {
        for (const minor of [0, 5, 1234, 999_999]) {
          assert.equal(parseMoney(priceText(minor, currency, locale), currency), minor, `${currency} ${locale} ${minor}`);
        }
      }
    }
  });
});
