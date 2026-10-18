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

// ZIF-133 fence. Kills: a short keyword matched as the start of an unrelated word ("gel" in the
// Dutch "Gelaatsbehandeling", a facial), and facial words missing from beauty. Real salon names.
// Migration 0040's backfill test runs the same table.
describe("real salon service names", () => {
  const cases = [
    [{ en: "Manicure" }, "nails"],
    [{ en: "Gel manicure" }, "nails"],
    [{ en: "Pedicure" }, "nails"],
    [{ nl: "Gelnagels" }, "nails"],
    [{ nl: "Gellak" }, "nails"],
    [{ nl: "Nagels lakken" }, "nails"],
    [{ pt: "Unhas de gel" }, "nails"],
    [{ pt: "Manicure e pedicure" }, "nails"],
    [{ en: "Facial" }, "beauty"],
    [{ en: "Face treatment" }, "beauty"],
    [{ nl: "Gelaatsbehandeling" }, "beauty"],
    [{ nl: "Gezichtsbehandeling" }, "beauty"],
    [{ pt: "Limpeza de pele" }, "beauty"],
    [{ pt: "Tratamento de rosto" }, "beauty"],
    [{ en: "Eyebrow wax" }, "beauty"],
    [{ nl: "Wimperlift" }, "beauty"],
    [{ en: "Beard trim" }, "hair"],
    [{ nl: "Knippen" }, "hair"],
    [{ pt: "Corte masculino" }, "hair"],
    [{ pt: "Massagem relaxante" }, "massage"],
    [{ nl: "Lakens wassen" }, "generic"], // "lak" is a whole word, not a prefix
  ];
  for (const [name, kind] of cases) {
    test(`${JSON.stringify(name)} -> ${kind}`, () => {
      assert.equal(placeholderFor(name), kind);
    });
  }
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
