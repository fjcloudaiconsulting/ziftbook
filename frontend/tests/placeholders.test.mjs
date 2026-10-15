// The name rule that pre-selects a service's placeholder (ZIF-133). Migration 0040 runs the same
// rule once over existing services; its test uses the same table.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

import { PLACEHOLDERS, placeholderFor } from "../lib/placeholders.ts";

describe("placeholderFor", () => {
  const cases = [
    [{ en: "Gel removal" }, "nails"],
    [{ nl: "Knippen" }, "hair"],
    [{ pt: "Depilação" }, "beauty"], // accents folded
    [{ pt: "DEPILAÇÃO" }, "beauty"], // capitals too
    [{ en: "_gel" }, "generic"], // _ is a word character, as in Postgres
    [{ en: "2gel" }, "generic"], // and so is a digit
    [{ en: "Hand massage" }, "massage"],
    [{ en: "Hair massage" }, "hair"], // kind order: hair before massage
    [{ en: "Consultation" }, "generic"],
    [{ nl: "Bagel" }, "generic"], // "gel" only counts at a word start
    [{ nl: "Wimper lift", en: "Manicure" }, "nails"], // kind order, not language order
    [{ en: "Wimper lift", nl: "Manicure" }, "nails"], // and not en-first order either
    [{ nl: "Föhnen" }, "hair"],
    [{ en: "Bridal make-up" }, "beauty"],
    [{ pt: "Limpeza de pele" }, "beauty"],
    [{ en: "" }, "generic"],
    [{}, "generic"],
  ];
  for (const [name, kind] of cases) {
    test(`${JSON.stringify(name)} -> ${kind}`, () => {
      assert.equal(placeholderFor(name), kind);
    });
  }
});

test("the pills come in the design's order", () => {
  assert.deepEqual(PLACEHOLDERS, ["generic", "nails", "hair", "beauty", "massage"]);
});

describe("every placeholder has a label in every language", () => {
  for (const locale of ["en", "nl", "pt"]) {
    test(locale, () => {
      const messages = JSON.parse(readFileSync(new URL(`../messages/${locale}.json`, import.meta.url), "utf8"));
      const kinds = messages.Console.services.form.placeholderKinds;
      assert.deepEqual(Object.keys(kinds).sort(), [...PLACEHOLDERS].sort());
    });
  }
});
