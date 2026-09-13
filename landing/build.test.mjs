import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const dir = new URL(".", import.meta.url).pathname;
execFileSync(process.execPath, ["build.mjs"], { cwd: dir });
const page = (locale) => readFileSync(`${dir}public/${locale}/index.html`, "utf8");

for (const [locale, lang] of [["en", "en"], ["nl", "nl"], ["pt", "pt"]]) {
  test(`${locale} page is fully rendered in its language`, () => {
    const html = page(locale);
    assert.match(html, new RegExp(`<html lang="${lang}">`));
    assert.doesNotMatch(html, /\{\{/, "no unreplaced placeholders");
    assert.match(html, new RegExp(`<link rel="canonical" href="https://ziftbook.com/${locale}/">`));
    assert.match(html, /hreflang="x-default" href="https:\/\/ziftbook.com\/en\/"/);
    assert.match(html, new RegExp(`<a href="/${locale}/"[^>]*aria-current="page"`));
  });
}

test("catalogs share exactly the same keys", () => {
  const keys = (locale) => Object.keys(JSON.parse(readFileSync(`${dir}strings/${locale}.json`, "utf8"))).sort();
  assert.deepEqual(keys("nl"), keys("en"));
  assert.deepEqual(keys("pt"), keys("en"));
});

test("strings are HTML-escaped", () => {
  assert.match(page("en"), /FJ Cloud &amp; AI Consulting/);
  assert.doesNotMatch(page("en"), /<script/);
});
