import assert from "node:assert/strict";
import { test } from "node:test";

import { compareCatalogs } from "./check-catalogs.mjs";

const en = { Home: { title: "Hello", greeting: "Hi {name}, it is {time}" }, footer: "Contact" };

test("identical structure and placeholders pass", () => {
  const nl = { Home: { title: "Hallo", greeting: "Hoi {name}, het is {time}" }, footer: "Contact" };
  assert.deepEqual(compareCatalogs(en, nl), []);
});

test("reports keys missing from the translation", () => {
  const nl = { Home: { title: "Hallo" }, footer: "Contact" };
  assert.deepEqual(compareCatalogs(en, nl), ["missing key Home.greeting"]);
});

test("reports keys that exist only in the translation", () => {
  const nl = { Home: { title: "Hallo", greeting: "Hoi {name}, het is {time}", extra: "x" }, footer: "Contact" };
  assert.deepEqual(compareCatalogs(en, nl), ["unexpected key Home.extra"]);
});

test("reports dropped or translated placeholders", () => {
  const pt = { Home: { title: "Olá", greeting: "Oi {nome}, são {time}" }, footer: "Contato" };
  assert.deepEqual(compareCatalogs(en, pt), ["placeholders differ at Home.greeting: expected {name,time}, got {nome,time}"]);
});

test("placeholder order may differ between languages", () => {
  const nl = { Home: { title: "Hallo", greeting: "Het is {time}, {name}" }, footer: "Contact" };
  assert.deepEqual(compareCatalogs(en, nl), []);
});

test("reports a string where the source has a group", () => {
  const nl = { Home: "Hallo", footer: "Contact" };
  assert.deepEqual(compareCatalogs(en, nl), ["type differs at Home: expected group, got string"]);
});

test("an ICU plural's branch text isn't mistaken for a placeholder", () => {
  const en2 = { count: "{count, plural, =1 {One day} other {# days}}" };
  const nl2 = { count: "{count, plural, =1 {Eén dag} other {# dagen}}" };
  assert.deepEqual(compareCatalogs(en2, nl2), []);
});

test("a real placeholder dropped inside an ICU plural branch is still caught", () => {
  const en2 = { count: "{count, plural, =1 {One day, {list}} other {# days, {list}}}" };
  const nl2 = { count: "{count, plural, =1 {Eén dag} other {# dagen}}" };
  assert.deepEqual(compareCatalogs(en2, nl2), ["placeholders differ at count: expected {count,list}, got {count}"]);
});

test("a one-word plural branch isn't mistaken for a placeholder", () => {
  const en2 = { unit: "{count, plural, one {day} other {days}}" };
  const nl2 = { unit: "{count, plural, one {dag} other {dagen}}" };
  assert.deepEqual(compareCatalogs(en2, nl2), []);
  assert.deepEqual(compareCatalogs(en2, { unit: "{n, plural, one {dag} other {dagen}}" }).length, 1);
});

test("prose like 'one {service}' outside a plural block still reports a dropped placeholder", () => {
  const en2 = { book: "Book one {service} now." };
  const nl2 = { book: "Boek nu." };
  assert.equal(compareCatalogs(en2, nl2).length, 1);
});

// ZIF-232 review: git merges a key added on two branches into a catalog twice without a conflict,
// and JSON.parse keeps the last copy. Wrong impl: duplicates left to JSON.parse.
test("reports a key twice in one object, not the same key in two objects", async () => {
  const { duplicateKeys } = await import("./check-catalogs.mjs");
  assert.deepEqual(duplicateKeys('{"a": {"x": 1, "y": 2, "x": 3}, "b": {"x": 1}}'), ["a.x"]);
  assert.deepEqual(duplicateKeys('{"a": "x: \\"y\\"", "b": {"a": 1}}'), []);
  assert.deepEqual(duplicateKeys('{"a": 1, "a": 2}'), ["a"]);
});

test("no catalog holds a key twice", async () => {
  const { duplicateKeys } = await import("./check-catalogs.mjs");
  const { readFileSync } = await import("node:fs");
  for (const dir of ["frontend/messages", "landing/strings"]) {
    for (const locale of ["en", "nl", "pt"]) {
      const text = readFileSync(new URL(`../${dir}/${locale}.json`, import.meta.url), "utf8");
      assert.deepEqual(duplicateKeys(text), [], `${dir}/${locale}.json`);
    }
  }
});

// Re-check NIT 7 FENCE. Wrong impl: a string scan with no end check, which loops for ever on an
// unterminated string and hangs CI instead of failing.
test("an unterminated string fails at once instead of hanging", async () => {
  const { duplicateKeys } = await import("./check-catalogs.mjs");
  assert.throws(() => duplicateKeys('{"a": "x}'), /unterminated string/);
  assert.throws(() => duplicateKeys('{"a": "x\\'), /unterminated string/);
});
