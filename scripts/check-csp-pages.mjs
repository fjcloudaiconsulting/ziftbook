// ZIF-236: backend/app/csp_reports.py logs a CSP violation's page as one of its PAGES route templates, never the
// path. Every page under frontend/app must have one, and every template a page, or a page's violations would be
// logged as "other". Checked by scripts/check-csp-pages.test.mjs, which `node --test "scripts/*.test.mjs"` runs
// against the real tree on every change that is not documentation.
import { readdirSync } from "node:fs";
import { join } from "node:path";

const PAGE_FILE = /^page\.(tsx|ts|jsx|js|mdx)$/;

/** Every directory under `appDir` holding a page file, as its path segments below `appDir`. Private folders (`_*`)
 * hold no routes and are skipped. */
export function pageDirs(appDir, readdir = (dir) => readdirSync(dir, { withFileTypes: true }), segments = []) {
  const found = [];
  for (const entry of readdir(join(appDir, ...segments))) {
    if (entry.isDirectory()) {
      if (!entry.name.startsWith("_")) found.push(...pageDirs(appDir, readdir, [...segments, entry.name]));
    } else if (PAGE_FILE.test(entry.name)) {
      found.push(segments);
    }
  }
  return found;
}

/** The route template of a page directory, as csp_reports.py writes it (`/[locale]/team/[memberId]`): route groups
 * flattened. Null for the `[...rest]` catch-all, which the API logs as "other". Throws on what the API's matcher does
 * not model (a page outside [locale], a parallel slot, an intercepting route, an optional catch-all), so a new kind
 * of route is classified on purpose. */
export function templateOf(segments) {
  if (segments[0] !== "[locale]") throw new Error(`csp_reports: a page outside [locale]: /${segments.join("/")}`);
  const kept = [];
  for (const segment of segments) {
    if (segment.startsWith("@") || /^\(\.{1,3}\)/.test(segment) || segment.startsWith("[[")) {
      throw new Error(`csp_reports: no template for the segment ${segment}`);
    }
    if (/^\(.*\)$/.test(segment)) continue;
    if (segment.startsWith("[...")) return null;
    kept.push(segment);
  }
  return `/${kept.join("/")}`;
}

/** The quoted templates of the PAGES tuple in csp_reports.py's source. */
export function cspPages(source) {
  const match = /^PAGES = \(([\s\S]*?)^\)/m.exec(source);
  if (!match) throw new Error("PAGES tuple not found in csp_reports.py");
  return [...match[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]);
}
