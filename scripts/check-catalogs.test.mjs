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
