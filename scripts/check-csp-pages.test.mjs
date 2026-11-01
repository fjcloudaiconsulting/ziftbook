import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

import { cspPages, pageDirs, templateOf } from "./check-csp-pages.mjs";

const APP_DIR = new URL("../frontend/app", import.meta.url).pathname;
const CSP_REPORTS = new URL("../backend/app/csp_reports.py", import.meta.url);

function fakeDir(names) {
  return names.map((name) => ({
    name: name.replace(/\/$/, ""),
    isDirectory: () => name.endsWith("/"),
  }));
}

describe("the CSP report page templates (ZIF-236)", () => {
  // Wrong implementation killed: a new page the API logs as "other", or a template for a page that is gone.
  test("fence: the API's PAGES are exactly the app's pages, both ways", () => {
    const pages = cspPages(readFileSync(CSP_REPORTS, "utf8"));
    const app = pageDirs(APP_DIR)
      .map(templateOf)
      .filter((t) => t !== null);
    assert.ok(pages.length > 0 && app.length > 0);
    assert.ok(pages.includes("/[locale]/[slug]/book"));
    assert.deepEqual([...new Set(app)].sort(), [...new Set(pages)].sort());
    assert.equal(new Set(pages).size, pages.length, "no template twice");
  });

  test("route groups are flattened, private folders skipped, the catch-all left out", () => {
    const tree = {
      app: fakeDir(["[locale]/", "version.json/", "layout.tsx"]),
      "app/[locale]": fakeDir(["(console)/", "_ui/", "[...rest]/", "[slug]/", "page.tsx"]),
      "app/[locale]/(console)": fakeDir(["team/"]),
      "app/[locale]/(console)/team": fakeDir(["page.tsx"]),
      "app/[locale]/_ui": fakeDir(["page.tsx"]),
      "app/[locale]/[...rest]": fakeDir(["page.tsx"]),
      "app/[locale]/[slug]": fakeDir(["book/", "page.tsx"]),
      "app/[locale]/[slug]/book": fakeDir(["page.tsx"]),
      "app/version.json": fakeDir(["route.ts"]),
    };
    const dirs = pageDirs("app", (dir) => tree[dir] ?? []);
    assert.deepEqual(
      dirs.map(templateOf).sort(),
      [null, "/[locale]", "/[locale]/[slug]", "/[locale]/[slug]/book", "/[locale]/team"].sort(),
    );
  });

  test("a page outside [locale], a parallel slot, an intercepting route or an optional catch-all fails loudly", () => {
    for (const segments of [["about"], ["[locale]", "@modal", "x"], ["[locale]", "(.)photo"], ["[locale]", "(..)x"], ["[locale]", "[[...all]]"], []]) {
      assert.throws(() => templateOf(segments), /csp_reports/, segments.join("/"));
    }
  });

  test("a missing PAGES tuple is an error, never an empty set", () => {
    assert.throws(() => cspPages("OTHER = ()\n"), /PAGES/);
    assert.deepEqual(cspPages('PAGES = (\n    "/[locale]",\n    "/[locale]/a",\n)\n'), ["/[locale]", "/[locale]/a"]);
  });
});
