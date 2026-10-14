import assert from "node:assert/strict";
import { describe, test } from "node:test";

import {
  newestReservedWords,
  reservedWords,
  topLevelRoutes,
  unreservedRoutes,
} from "./check-reserved-routes.mjs";

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

  // Fence (ZIF-70): a migration that restores an older list keeps it as OLD_RESERVED. Kills the
  // unanchored regex, which matches inside OLD_RESERVED first.
  test("ignores an OLD_RESERVED block above the RESERVED one", () => {
    const source = `OLD_RESERVED = """'x'"""\nRESERVED = """'y'"""\n`;
    assert.deepEqual(reservedWords(source), ["y"]);
  });
});

describe("newestReservedWords (ZIF-70)", () => {
  const files = {
    "0030_tenant_slug.py": `RESERVED = """\n  'booking','admin'\n"""\n`,
    "0031_other.py": `revision = "0031"\n`,
    "0035_reserve_setup_slug.py": `OLD_RESERVED = """'booking','admin'"""\nRESERVED = """\n  'booking','admin','setup'\n"""\n`,
  };
  const readFile = (path) => files[path.split("/").pop()];

  // Fence: kills a check still hard-wired to 0030, which would let a route reserved by a later
  // migration fail (or a later un-reservation pass).
  test("reads the newest migration that defines RESERVED", () => {
    const readdir = () => Object.keys(files);
    assert.ok(newestReservedWords("versions", readdir, readFile).includes("setup"));
  });

  test("falls back to an older one when the newest is gone", () => {
    const readdir = () => Object.keys(files).filter((f) => !f.startsWith("0035"));
    assert.deepEqual(newestReservedWords("versions", readdir, readFile), ["booking", "admin"]);
  });
});

describe("fence: a real route directory added without reserving it is caught (F8)", () => {
  // Wrong implementation killed: comparing against a stale/incomplete reserved list (e.g. one
  // that forgot a route), which would let a future business slug shadow a real page.
  test("every current top-level route under app/[locale] is in the newest reserved list", () => {
    const routes = topLevelRoutes("frontend/app/[locale]");
    const reserved = newestReservedWords("backend/migrations/versions");
    assert.deepEqual(unreservedRoutes(routes, reserved), []);
  });

  test("a route missing from an incomplete reserved list is reported", () => {
    assert.deepEqual(unreservedRoutes(["booking", "new-route"], ["booking"]), ["new-route"]);
  });
});
