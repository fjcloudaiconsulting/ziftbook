// The Terms and Privacy pages (ZIF-147): every language has the same shape as English, and none
// ships with an owner fact still unfilled.
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { LEGAL } from "../lib/legal/index.ts";

describe("legal pages", () => {
  test("no [OWNER: ...] placeholder is left in any language", () => {
    const left = Object.entries(LEGAL).flatMap(([locale, docs]) =>
      [...JSON.stringify(docs).matchAll(/\[OWNER:[^\]]*\]/g)].map((m) => `${locale}: ${m[0]}`),
    );
    assert.deepEqual(left, []);
  });

  test("nl and pt have English's sections, in the same order and shape", () => {
    const shape = (docs) =>
      Object.values(docs).map((doc) => doc.sections.map((s) => s.body.map((b) => (Array.isArray(b) ? b.length : "p"))));
    for (const locale of ["nl", "pt"]) assert.deepEqual(shape(LEGAL[locale]), shape(LEGAL.en), locale);
  });
});
