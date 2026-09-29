import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

import { reservedWords, topLevelRoutes, unreservedRoutes } from "./check-reserved-routes.mjs";

function fakeDir(names) {
  return names.map((name) => ({
    name: name.replace(/\/$/, ""),
    isDirectory: () => name.endsWith("/") || !name.includes("."),
  }));
}

describe("topLevelRoutes (F8)", () => {
  test("route groups are flattened, private folders and dynamic segments are skipped", () => {
    const tree = { "app/[locale]": fakeDir(["_ui/", "booking/", "(console)/", "[slug]/", "layout.tsx"]) };
    const readdir = (dir) => (dir === "app/[locale]" ? tree["app/[locale]"] : fakeDir(["calendar/", "clients/"]));
    assert.deepEqual(topLevelRoutes("app/[locale]", readdir).sort(), ["booking", "calendar", "clients"]);
  });
});

describe("reservedWords", () => {
  test("reads the quoted words out of migration 0030's RESERVED block", () => {
    const source = `RESERVED = """\n  'booking','sign-in',\n  'api','admin'\n"""\n`;
    assert.deepEqual(reservedWords(source), ["booking", "sign-in", "api", "admin"]);
  });
});

describe("fence: a real route directory added without reserving it is caught (F8)", () => {
  // Wrong implementation killed: comparing against a stale/incomplete reserved list (e.g. one
  // that forgot a route), which would let a future business slug shadow a real page.
  test("every current top-level route under app/[locale] is in migration 0030's list", () => {
    const routes = topLevelRoutes("frontend/app/[locale]");
    const reserved = reservedWords(readFileSync("backend/migrations/versions/0030_tenant_slug.py", "utf8"));
    assert.deepEqual(unreservedRoutes(routes, reserved), []);
  });

  test("a route missing from an incomplete reserved list is reported", () => {
    assert.deepEqual(unreservedRoutes(["booking", "new-route"], ["booking"]), ["new-route"]);
  });
});
