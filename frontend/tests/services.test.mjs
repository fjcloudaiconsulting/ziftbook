// Pure logic behind the service create/edit form: which name a reader sees, the create/edit PATCH
// body (and its field errors), and the default-gap hint. Kept free of React so node --test can
// run it directly.
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { parseMoney } from "../lib/money.ts";
import { defaultBuffer, serviceBody, serviceName } from "../lib/services.ts";

function body(form, options) {
  return serviceBody(form, options, parseMoney);
}

describe("serviceName", () => {
  test("only the business language present: reads that one, not en", () => {
    assert.equal(serviceName({ pt: "Unhas" }, "en", "nl"), "Unhas");
  });

  test("reader's language missing, business language present, plus another: reads the business language, not first-present", () => {
    assert.equal(serviceName({ pt: "Unhas", nl: "Nagels" }, "en", "nl"), "Nagels");
  });

  test("reader's own language wins when present", () => {
    assert.equal(serviceName({ en: "Nails", nl: "Nagels" }, "en", "nl"), "Nails");
  });
});

describe("serviceBody", () => {
  const base = { businessLanguage: "nl", mode: "create" };

  test("blank or whitespace-only languages are omitted, never sent as \"\"", () => {
    const result = body(
      { name: { nl: "Manicure", en: "  ", pt: "" }, description: {}, price: "35,00", duration: "45", gap: "default" },
      { ...base },
    );
    assert.ok(!("errors" in result));
    assert.deepEqual(result.body.name, { nl: "Manicure" });
  });

  test("edit that empties pt keeps every other language, and sends name without pt", () => {
    const result = body(
      { name: { nl: "Manicure", en: "Manicure", pt: "" }, description: {}, price: "35,00", duration: "45", gap: "default" },
      { businessLanguage: "nl", mode: "edit" },
    );
    assert.ok(!("errors" in result));
    assert.deepEqual(result.body.name, { nl: "Manicure", en: "Manicure" });
  });

  test('"use the business default" sends buffer_minutes: null, never 0', () => {
    const result = body(
      { name: { nl: "Manicure" }, description: {}, price: "35,00", duration: "45", gap: "default" },
      { ...base },
    );
    assert.ok(!("errors" in result));
    assert.equal(result.body.buffer_minutes, null);
  });

  test("edit body has only name, price and duration_minutes: never description or buffer_minutes", () => {
    const result = body(
      { name: { nl: "Manicure" }, description: { nl: "would be dropped" }, price: "35,00", duration: "45", gap: "fixed", fixedGap: "10" },
      { businessLanguage: "nl", mode: "edit" },
    );
    assert.ok(!("errors" in result));
    assert.deepEqual(Object.keys(result.body).sort(), ["duration_minutes", "name", "price"]);
  });

  test("create without a business-language name: nameRequired", () => {
    const result = body(
      { name: { nl: "", en: "Manicure" }, description: {}, price: "35,00", duration: "45", gap: "default" },
      { ...base },
    );
    assert.equal(result.errors?.name, "nameRequired");
  });

  test("edit of {pt: Unhas} with business nl is a valid body: the create rule does not apply", () => {
    const result = body(
      { name: { nl: "", pt: "Unhas" }, description: {}, price: "35,00", duration: "45", gap: "default" },
      { businessLanguage: "nl", mode: "edit" },
    );
    assert.ok(!("errors" in result), JSON.stringify(result));
    assert.deepEqual(result.body.name, { pt: "Unhas" });
  });

  test("edit with every language blank: still nameRequired", () => {
    const result = body(
      { name: { nl: "", en: "", pt: "" }, description: {}, price: "35,00", duration: "45", gap: "default" },
      { businessLanguage: "nl", mode: "edit" },
    );
    assert.equal(result.errors?.name, "nameRequired");
  });

  test("duration outside 5..720 is a field error", () => {
    const tooShort = body(
      { name: { nl: "Manicure" }, description: {}, price: "35,00", duration: "4", gap: "default" },
      { ...base },
    );
    assert.equal(tooShort.errors?.duration, "durationRange");
    const tooLong = body(
      { name: { nl: "Manicure" }, description: {}, price: "35,00", duration: "721", gap: "default" },
      { ...base },
    );
    assert.equal(tooLong.errors?.duration, "durationRange");
  });

  test("a fixed gap outside 0..240 is a field error", () => {
    const result = body(
      { name: { nl: "Manicure" }, description: {}, price: "35,00", duration: "45", gap: "fixed", fixedGap: "241" },
      { ...base },
    );
    assert.equal(result.errors?.gap, "gapRange");
  });

  test("an unparsable price is a field error", () => {
    const result = body(
      { name: { nl: "Manicure" }, description: {}, price: "not a number", duration: "45", gap: "default" },
      { ...base },
    );
    assert.equal(result.errors?.price, "priceInvalid");
  });
});

describe("defaultBuffer", () => {
  test("44 minutes at 10% is 5 (ceil, not round)", () => {
    assert.equal(defaultBuffer(44, 10), 5);
  });

  test("40 minutes at 10% is 4 (an exact value stays exact under ceil)", () => {
    assert.equal(defaultBuffer(40, 10), 4);
  });
});
