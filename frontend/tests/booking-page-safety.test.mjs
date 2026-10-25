// ZIF-146 (security B3): the booking, checkout, manage, invite and account pages render merchant-controlled text on
// the same origin as GET /api/account, which answers a signed-in person's name, email and phone. These fences keep
// script off them: no string ever turned into markup, no href, src, action or navigation built from API text except
// through safeLink, no style attribute the enforced policy would refuse, no cookie read on the server, and every one
// of them guarded against a client-side navigation from a report-only page.
import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { describe, test } from "node:test";

import { ESLint } from "eslint";

import { PERSONAL_ROUTES } from "../lib/personal-pages.ts";

const WEB_DIR = new URL("..", import.meta.url).pathname;
const LOCALE_DIR = join(WEB_DIR, "app", "[locale]");
// The pages personalPage() enforces: a business's pages ([slug]) and every personal route that exists.
const PERSONAL_DIRS = ["[slug]", ...PERSONAL_ROUTES].map((d) => join(LOCALE_DIR, d)).filter(existsSync);

function sources(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return sources(path);
    return /\.(tsx?|mts)$/.test(name) ? [path] : [];
  });
}

/** The personal pages' own files, and every shared file under app/[locale]/_ui they import, followed through. */
function personalFiles() {
  const seen = new Set();
  const queue = PERSONAL_DIRS.flatMap(sources);
  while (queue.length) {
    const file = queue.pop();
    if (seen.has(file)) continue;
    seen.add(file);
    for (const [, spec] of readFileSync(file, "utf8").matchAll(/from\s+["']([^"']+)["']/g)) {
      const base = spec.startsWith("@/app/") ? join(WEB_DIR, spec.slice(2)) : spec.startsWith(".") ? resolve(dirname(file), spec) : null;
      if (!base || !base.startsWith(join(LOCALE_DIR, "_ui"))) continue;
      for (const candidate of [base, `${base}.tsx`, `${base}.ts`]) {
        if (existsSync(candidate) && statSync(candidate).isFile()) queue.push(candidate);
      }
    }
  }
  return [...seen].sort();
}

const FILES = personalFiles();
const name = (file) => file.slice(LOCALE_DIR.length + 1);

describe("no string becomes markup", () => {
  const lint = new ESLint({ cwd: WEB_DIR });

  test("fence: the lint refuses every string-to-markup sink", async () => {
    // Wrong implementation killed: a page that renders a merchant's description as HTML, by any of these routes.
    for (const code of [
      "export const A = ({ html }: { html: string }) => <div dangerouslySetInnerHTML={{ __html: html }} />;",
      "export const A = ({ html }: { html: string }) => <iframe srcDoc={html} />;",
      "export function a(el: HTMLElement, t: string) { el.innerHTML = t; }",
      'export function a(el: HTMLElement, t: string) { el["innerHTML"] = t; }',
      "export function a(el: HTMLElement, t: string) { Object.assign(el, { innerHTML: t }); }",
      "export function a(el: HTMLElement, t: string) { el.outerHTML = t; }",
      "export function a(el: HTMLElement, t: string) { el.insertAdjacentHTML('beforeend', t); }",
      "export function a(t: string) { return new DOMParser().parseFromString(t, 'text/html'); }",
      "export function a(el: Element, t: string) { (el as unknown as { setHTMLUnsafe(s: string): void }).setHTMLUnsafe(t); }",
      "export function a(t: string) { document.write(t); }",
    ]) {
      const [result] = await lint.lintText(code, { filePath: join(LOCALE_DIR, "[slug]", "probe.tsx") });
      assert.ok(result.errorCount > 0, code);
    }
  });

  test("today's personal pages, and the shared parts they use, have no such sink at all", () => {
    assert.ok(FILES.some((f) => f.includes("/_ui/")), "the walk follows imports into _ui");
    for (const file of FILES) {
      assert.doesNotMatch(readFileSync(file, "utf8"), /dangerouslySetInnerHTML|innerHTML|outerHTML|insertAdjacentHTML|srcDoc|parseFromString/, name(file));
    }
  });
});

// A same-origin path: a template that starts with one "/" and not "//" or "/\" (another origin to a browser).
// A template whose first segment is interpolated, `/${x}`, is "//host" when x starts with "/": only the values
// whose shape is known qualify (fold re-verify NB-5): the locale (routing's own list) and a business's slug
// (^[a-z0-9]+(-[a-z0-9]+)*$, enforced by the database and slugLooksValid).
const SAME_ORIGIN = /^`\/(?![/\\]|\$\{)|^`\/\$\{(?:locale|slug|page\.slug)\}/;
const SAME_ORIGIN_LITERAL = /^["']\/(?![/\\])/;

// An href, src, action or navigation on these pages that is neither a same-origin path nor a safeLink() call, each
// listed with why it cannot run script or leave the site.
const ALLOWED = new Map([
  // The service photo's media URL on an <img>: an image never runs script, and img-src limits it to the media origin.
  ["[slug]/services-page.tsx", new Set(["svc.photo.detail_url"])],
  // NewTab's own prop: every caller passes a literal path ("/terms", "/privacy").
  ["[slug]/footer.tsx", new Set(["href"])],
  // The language switcher: the current page's own path (usePathname), with only the locale changed.
  ["_ui/header.tsx", new Set(["pathname"])],
  // A service's photo thumb URL on an <img>, as on the services page: never runs script, img-src limits it.
  ["_ui/service-tile.tsx", new Set(["photo"])],
]);

/** Every JSX expression given to one of these attributes, braces balanced. */
function attributeExpressions(text, names) {
  const found = [];
  for (const m of text.matchAll(new RegExp(`\\b(?:${names})=\\{`, "g"))) {
    let depth = 1;
    let i = m.index + m[0].length;
    const start = i;
    for (; i < text.length && depth > 0; i++) {
      if (text[i] === "{") depth++;
      else if (text[i] === "}") depth--;
    }
    found.push(text.slice(start, i - 1).trim());
  }
  return found;
}

function unsafeLinks(file, text) {
  const allowed = ALLOWED.get(file) ?? new Set();
  return attributeExpressions(text, "href|src|action|formAction").filter(
    (expr) => !SAME_ORIGIN.test(expr) && !/^safeLink\(/.test(expr) && !allowed.has(expr),
  );
}

// Navigations whose argument is built in code that only ever makes a same-origin path, each with where it is built.
const NAVIGATION_ALLOWED = new Map([
  // `base` is `/${page.slug}` (services-page.tsx); checkoutUrl() returns `/${slug}/book?...` (lib/booking-page.ts).
  ["[slug]/services-page.tsx", new Set(["base", "checkoutUrl(page.slug"])],
]);

function unsafeNavigations(file, text) {
  const allowed = NAVIGATION_ALLOWED.get(file) ?? new Set();
  // location.assign/replace, window.open and a bare open() with an argument (not a local function's
  // declaration or a call with none), router.push/replace.
  const calls = /(?:\blocation\.(?:assign|replace)|\bwindow\.open|(?<![\w.]|function )open|\brouter\.(?:push|replace))\(\s*([^,)]+)/g;
  // location.href = ..., and window.location = ... / document.location = ... / location = ...
  const sets = /(?<![\w.])(?:(?:window|document)\.)?location(?:\.href)?\s*=(?!=)\s*([^;\n]+)/g;
  return [...text.matchAll(calls), ...text.matchAll(sets)]
    .map((m) => m[1].trim())
    .filter((expr) => !SAME_ORIGIN.test(expr) && !SAME_ORIGIN_LITERAL.test(expr) && !/^safeLink\(/.test(expr) && !allowed.has(expr));
}

function spreads(text) {
  // An attribute spread ({...x} after a space inside a tag), not an object spread inside a prop's value.
  return [...text.matchAll(/<[A-Za-z][\w.]*\b[^<>]*?\s\{\s*\.\.\.\s*([^}]+)\}/g)].map((m) => m[1].trim());
}

describe("no link built from API text", () => {
  test("fence: every dynamic href, src, action and navigation is a same-origin path, a safeLink() or a listed exception", () => {
    // Wrong implementation killed: <a href={page.contact_url}> on the booking page (javascript: runs on click),
    // href={`//${page.domain}`} (another origin), or router.push(page.website).
    for (const file of FILES) {
      const text = readFileSync(file, "utf8");
      assert.deepEqual(unsafeLinks(name(file), text), [], name(file));
      assert.deepEqual(unsafeNavigations(name(file), text), [], name(file));
    }
  });

  test("fence: no props are spread onto an element, where an href could ride in unseen", () => {
    for (const file of FILES) assert.deepEqual(spreads(readFileSync(file, "utf8")), [], name(file));
  });

  test("the checks themselves catch each way out", () => {
    const probe = [
      "<a href={page.contact_url}>x</a>",
      "<a href={`//${page.domain}/x`}>x</a>",
      "<a href={`/\\\\evil.example`}>x</a>",
      "<a href={`/${slug}`}>ok</a>",
      "<a href={safeLink(page.site)}>ok</a>",
    ].join(" ");
    assert.deepEqual(unsafeLinks("probe.tsx", probe), ["page.contact_url", "`//${page.domain}/x`", "`/\\\\evil.example`"]);
    const nav = [
      "router.push(page.website);",
      "window.open(url, '_blank');",
      "location.href = page.site;",
      "window.location = page.home;",
      "document.location = page.home2;",
      "open(page.popup);",
      "router.replace(`/${slug}`);",
      "router.push('/');",
    ].join(" ");
    assert.deepEqual(unsafeNavigations("probe.tsx", nav).sort(), ["page.home", "page.home2", "page.popup", "page.site", "page.website", "url"]);
    // Fold re-verify NB-5: an interpolated first segment is "//host" when it starts with "/".
    assert.deepEqual(unsafeLinks("probe.tsx", "<a href={`/${page.path}`}>x</a> <a href={`/${locale}/${slug}`}>ok</a>"), ["`/${page.path}`"]);
    assert.deepEqual(spreads("<a {...link}>x</a> <Link {...props} />"), ["link", "props"]);
  });
});

describe("what the enforced policy needs", () => {
  test("fence: no style attribute on these pages, which the enforced policy refuses in their HTML", () => {
    // Wrong implementation killed: style={{ whiteSpace: "pre-line" }} on the manage page: server-rendered as a
    // style="" attribute, blocked, and the text it formats collapses.
    for (const file of FILES) assert.doesNotMatch(readFileSync(file, "utf8"), /\bstyle=\{/, name(file));
  });

  test("fence: every personal route renders the guard against a client-side navigation from a report-only page", () => {
    // Wrong implementation killed: a booking page reached by <Link> from the console, rendered inside the console
    // document's report-only policy.
    for (const dir of PERSONAL_DIRS) {
      const layout = join(dir, "layout.tsx");
      assert.ok(existsSync(layout), `${name(dir)} has a layout`);
      assert.match(readFileSync(layout, "utf8"), /<CspGuard \/>/, name(layout));
    }
  });
});

describe("no account data on the server", () => {
  test("fence: these pages read no cookie, and forward no request headers, when they render", () => {
    // Wrong implementation killed: a server-rendered prefill (cookies() in page.tsx, or the loader forwarding
    // await headers() wholesale, cookie and all): the person's details would be in HTML an edge cache could keep.
    for (const file of FILES) {
      const text = readFileSync(file, "utf8");
      assert.doesNotMatch(text, /\bcookies\s*\(/, name(file));
      assert.doesNotMatch(text, /["']cookie["']/i, name(file));
      for (const [use] of text.matchAll(/.{0,20}\bheaders\(\).{0,40}/g)) {
        assert.ok(
          /clientIp\(await headers\(\)\)/.test(use) || /\(await headers\(\)\)\.has\("content-security-policy"\)/.test(use),
          `${name(file)}: ${use.trim()}`,
        );
      }
    }
  });
});
