// ZIF-146 (security B3): the booking, checkout, manage and account pages render merchant-controlled text on
// the same origin as GET /api/account, which answers a signed-in person's name, email and phone. These
// fences keep script off them: an enforced policy on exactly those paths, no string ever turned into
// markup, no href or src built from API text except through safeLink, and no cookie read on the server.
import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, test } from "node:test";

import { ESLint } from "eslint";


const WEB_DIR = new URL("..", import.meta.url).pathname;
const LOCALE_DIR = join(WEB_DIR, "app", "[locale]");
// The pages personalPage() enforces: a business's pages ([slug]), the manage page and the account pages.
const PERSONAL_DIRS = ["[slug]", "booking", "account"].map((d) => join(LOCALE_DIR, d)).filter(existsSync);

function sources(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return sources(path);
    return /\.(tsx?|mts)$/.test(name) ? [path] : [];
  });
}

describe("no string becomes markup", () => {
  const lint = new ESLint({ cwd: WEB_DIR });

  test("fence: the lint refuses dangerouslySetInnerHTML, innerHTML and the other string-to-markup sinks", async () => {
    // Wrong implementation killed: a page that renders a merchant's description as HTML.
    for (const code of [
      "export const A = ({ html }: { html: string }) => <div dangerouslySetInnerHTML={{ __html: html }} />;",
      "export function a(el: HTMLElement, t: string) { el.innerHTML = t; }",
      "export function a(el: HTMLElement, t: string) { el.outerHTML = t; }",
      "export function a(el: HTMLElement, t: string) { el.insertAdjacentHTML('beforeend', t); }",
      "export function a(t: string) { document.write(t); }",
    ]) {
      const [result] = await lint.lintText(code, { filePath: join(LOCALE_DIR, "[slug]", "probe.tsx") });
      assert.ok(result.errorCount > 0, code);
    }
  });

  test("today's booking and manage pages have no such sink at all", () => {
    for (const file of PERSONAL_DIRS.flatMap(sources)) {
      assert.doesNotMatch(readFileSync(file, "utf8"), /dangerouslySetInnerHTML|innerHTML|outerHTML|insertAdjacentHTML/, file);
    }
  });
});

// An href, src or action on these pages: a same-origin path built from a template that starts with "/",
// a safeLink() call, or one of these, each with why it cannot run script.
const ALLOWED = new Map([
  // The service photo's media URL on an <img>: an image never runs script, and img-src limits it to the media origin.
  ["services-page.tsx", new Set(["svc.photo.detail_url"])],
  // NewTab's own prop: every caller passes a literal path ("/terms", "/privacy").
  ["footer.tsx", new Set(["href"])],
]);

function unsafeLinks(file, text) {
  const allowed = ALLOWED.get(file.split("/").at(-1)) ?? new Set();
  return [...text.matchAll(/\b(?:href|src|action|formAction)=\{([^}]*(?:\}[^}]*)*?)\}(?=[\s/>])/g)]
    .map((m) => m[1].trim())
    .filter((expr) => !expr.startsWith("`/") && !/^safeLink\(/.test(expr) && !allowed.has(expr));
}

describe("no link built from API text", () => {
  test("fence: every dynamic href, src or action is a same-origin path, a safeLink() or a listed exception", () => {
    // Wrong implementation killed: <a href={page.contact_url}> on the booking page (javascript: runs on click).
    for (const file of PERSONAL_DIRS.flatMap(sources)) {
      assert.deepEqual(unsafeLinks(file, readFileSync(file, "utf8")), [], file);
    }
  });

  test("the check itself catches an API value in an href", () => {
    const probe = '<a href={page.contact_url}>x</a> <a href={`/${slug}`}>y</a> <a href={safeLink(page.site)}>z</a>';
    assert.deepEqual(unsafeLinks("probe.tsx", probe), ["page.contact_url"]);
  });
});

describe("no account data on the server", () => {
  test("fence: the booking, manage and account pages read no cookie when they render", () => {
    // Wrong implementation killed: a server-rendered prefill (cookies() in page.tsx, or the cookie header
    // forwarded by the loader): the person's details would be in HTML an edge cache could keep.
    for (const file of PERSONAL_DIRS.flatMap(sources)) {
      const text = readFileSync(file, "utf8");
      assert.doesNotMatch(text, /\bcookies\s*\(/, file);
      assert.doesNotMatch(text, /["']cookie["']/i, file);
    }
  });
});
