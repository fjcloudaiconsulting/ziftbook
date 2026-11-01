// ZIF-102: where the language rules are wired in. The pure rules are fenced in locale.test.mjs; these
// guards fail when a page stops calling them, which no unit test of a helper can see.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");

describe("a signed-in viewer's own language on the booking page (ruling 4: on the booking page too)", () => {
  test("fence: every header of the service list and the checkout is the signed-in-aware switcher", () => {
    // Wrong implementation killed: a bare <LanguageSwitcher /> there, which never reads the viewer,
    // never moves the page to their language and never saves a pick.
    for (const page of ["../app/[locale]/[slug]/page.tsx", "../app/[locale]/[slug]/book/page.tsx"]) {
      const src = read(page);
      assert.doesNotMatch(src, /<LanguageSwitcher\b/, page);
      const headers = src.match(/<header className=\{styles\.minimalHeader\}>[\s\S]*?<\/header>/g) ?? [];
      assert.ok(headers.length >= 2, page);
      for (const header of headers) assert.match(header, /<OwnLanguageSwitcher\b/, page);
    }
  });

  test("fence: the checkout holds the move while it resumes a booking from its address", () => {
    // Wrong implementation killed: moving the page mid-resume (a Stripe return), which remounts the
    // checkout and starts its status check a second time.
    const src = read("../app/[locale]/[slug]/book/page.tsx");
    for (const header of src.match(/<header className=\{styles\.minimalHeader\}>[\s\S]*?<\/header>/g) ?? []) {
      assert.match(header, /<OwnLanguageSwitcher hold=\{pick\.has\("booking"\)\} \/>/);
    }
  });

  test("fence: the signed-out header uses the same switcher", () => {
    assert.match(read("../app/[locale]/_ui/header.tsx"), /export function Header\(\)[\s\S]*<OwnLanguageSwitcher \/>/);
  });
});
