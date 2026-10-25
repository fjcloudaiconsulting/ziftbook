// ZIF-102, ruling 5: the languages the app speaks are one list per side. The backend's is its
// Locale type (backend/app/locales.py), which the API contract carries as an enum; the frontend
// holds two copies, next-intl's routing and lib/services.ts's LOCALES (which may not import a
// sibling). Adding a language: CONTRIBUTING.md, "Translations".
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { routing } from "../i18n/routing.ts";
import { LOCALES } from "../lib/services.ts";

const api = JSON.parse(readFileSync(new URL("../../backend/openapi.json", import.meta.url), "utf8"));

// Fence. Wrong impl: a language added on one side only (the backend's Locale, routing, or LOCALES).
test("routing.locales, LOCALES and the API's Locale enum are the same list", () => {
  const [backend] = api.components.schemas.LocaleIn.properties.locale.anyOf.filter((s) => s.enum);
  assert.deepEqual([...routing.locales], backend.enum);
  assert.deepEqual(LOCALES, backend.enum);
});

// Ruling 5: a guest whose browser speaks none of them gets English.
test("the default language is English, and it is one of the list", () => {
  assert.equal(routing.defaultLocale, "en");
  assert.ok(routing.locales.includes(routing.defaultLocale));
});
