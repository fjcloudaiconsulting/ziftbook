import assert from "node:assert/strict";
import { test } from "node:test";

import { documentedNames, pydanticFieldNames, usedNames } from "./check-env-doc.mjs";

test("documented names come from the CONTRIBUTING.md table", () => {
  const table = "| `ZIF_APP_URL` | worker | ... |\n| not a row |\n| `ZIF_LOG_LEVEL` | api | ... |\n";
  assert.deepEqual(documentedNames(table), new Set(["ZIF_APP_URL", "ZIF_LOG_LEVEL"]));
});

test("pydantic settings fields map to ZIF_<FIELD>", () => {
  const source = 'class Settings(BaseSettings):\n    app_version: str = "dev"\n    database_url: str\n';
  assert.deepEqual(pydanticFieldNames(source), ["ZIF_APP_VERSION", "ZIF_DATABASE_URL"]);
});

test("usedNames finds every distinct ZIF_ token in a file", () => {
  const source = "ZIF_DATABASE_URL: ${ZIF_APP_PASSWORD:?set it}\nZIF_DATABASE_URL again\n";
  assert.deepEqual(usedNames(source), ["ZIF_DATABASE_URL", "ZIF_APP_PASSWORD"]);
});

test("an undocumented name used in code fails the check (RED without its row)", () => {
  const table = "| `ZIF_APP_URL` | worker | ... |\n";
  const documented = documentedNames(table);
  const used = usedNames("ZIF_APP_URL and ZIF_UNDOCUMENTED_THING here");
  const missing = used.filter((name) => !documented.has(name));
  assert.deepEqual(missing, ["ZIF_UNDOCUMENTED_THING"]);
});
