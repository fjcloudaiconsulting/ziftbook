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

describe("the moves to the person's own language are wired in", () => {
  test("fence: the public hook resolves the viewer's language and moves the page to it", () => {
    // Wrong implementation killed: the move left out, so the resolveLocale fences pass while no page
    // ever changes language.
    const src = read("../app/[locale]/_ui/header.tsx");
    const hook = src.slice(src.indexOf("export function useOwnLanguage("), src.indexOf("export function OwnLanguageSwitcher("));
    assert.match(hook, /const own = resolveLocale\(\{[\s\S]*surface: "public"[\s\S]*\}\);\s*if \(own !== locale\) router\.replace\(`\$\{pathname\}\$\{window\.location\.search\}`, \{ locale: own \}\);/);
  });

  test("fence: the console's shell resolves the person's language and moves the page to it", () => {
    // Wrong implementation killed: a console that never leaves the URL's language, so a Portuguese
    // speaker in a Dutch salon keeps a Dutch console (the ticket's visible outcome).
    const src = read("../app/[locale]/_ui/console.tsx");
    assert.match(src, /const language = session\s*\? resolveLocale\(\{[\s\S]*surface: "console"[\s\S]*\}\)\s*: locale;/);
    assert.match(src, /if \(language !== locale\) router\.replace\(`\$\{pathname\}\$\{window\.location\.search\}`, \{ locale: language \}\);/);
    assert.match(src, /if \(language !== locale\) return null;/);
  });
});

const hook = () => {
  const src = read("../app/[locale]/_ui/header.tsx");
  return src.slice(src.indexOf("export function useOwnLanguage("), src.indexOf("export function OwnLanguageSwitcher("));
};

describe("the public hook's own safeguards, where they are wired", () => {
  test("fence: the hook honours `hold` before it can move the page", () => {
    // Wrong implementation killed: `hold` passed by the checkout but ignored, so a signed-in viewer
    // back from Stripe is moved mid-resume and the checkout remounts and checks again.
    assert.match(hook(), /setViewer\([^;]*\);\s*if \(hold\) return;\s*const own = resolveLocale\(/);
  });
});
