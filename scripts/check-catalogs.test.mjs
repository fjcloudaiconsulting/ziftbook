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
