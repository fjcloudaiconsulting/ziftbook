// ZIF-78: the plan editor's conversions between what an admin types and what the API stores.
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { formatPercent, formFromValues, valuesFromForm } from "../lib/admin-plans.ts";

const registry = [
  { key: "places", kind: "count", default: 1, description: "" },
  { key: "fee", kind: "percent", default: 0, description: "" },
  { key: "sync", kind: "flag", default: false, description: "" },
];

describe("valuesFromForm", () => {
  // FENCE units: a percent typed as "12.5" is stored as 1250 basis points, never 12 or 12.5.
  test("percent becomes basis points, counts whole numbers, flags booleans", () => {
    assert.deepEqual(valuesFromForm({ places: "5", fee: "12.5", sync: true }, registry), { values: { places: 5, fee: 1250, sync: true } });
    assert.deepEqual(valuesFromForm({ places: "0", fee: "0,05", sync: false }, registry), { values: { places: 0, fee: 5, sync: false } });
    assert.deepEqual(valuesFromForm({ places: "1", fee: "100", sync: false }, registry), { values: { places: 1, fee: 10000, sync: false } });
  });

  // FENCE refusal: anything the API would refuse names the field instead of being sent.
  test("refuses negatives, fractions, blanks, three decimals and over 100 percent", () => {
    for (const [field, bad] of [["places", "-1"], ["places", "1.5"], ["places", ""], ["fee", "1.234"], ["fee", "100.01"], ["fee", "-1"], ["fee", "abc"]]) {
      const form = { places: "1", fee: "0", sync: false, [field]: bad };
      assert.deepEqual(valuesFromForm(form, registry), { error: field }, `${field}=${bad}`);
    }
  });
});

describe("formFromValues", () => {
  test("shows stored values, defaults for the rest, percent as typed", () => {
    assert.deepEqual(formFromValues({ fee: 1250 }, registry), { places: "1", fee: "12.5", sync: false });
    assert.deepEqual(formFromValues({ fee: 1000, sync: true, places: 0 }, registry), { places: "0", fee: "10", sync: true });
  });
});

describe("formatPercent", () => {
  test("basis points as a percentage in the locale", () => {
    assert.equal(formatPercent(0, "nl"), "0%");
    assert.equal(formatPercent(1250, "en"), "12.5%");
    assert.equal(formatPercent(1250, "nl"), "12,5%");
  });
});
