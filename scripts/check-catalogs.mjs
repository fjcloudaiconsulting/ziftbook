// Checks every translation catalog against its English source: same keys, same {placeholders}.
// Run: node scripts/check-catalogs.mjs
import { readFileSync } from "node:fs";

const CATALOG_SETS = [
  { dir: "apps/web/messages", source: "en", translations: ["nl", "pt"] },
  { dir: "landing/strings", source: "en", translations: ["nl", "pt"] },
];

const placeholders = (text) => [...new Set([...text.matchAll(/\{(\w+)/g)].map((m) => m[1]))].sort().join(",");
const kind = (value) => (typeof value === "string" ? "string" : "group");

export function compareCatalogs(source, translation, prefix = "") {
  const problems = [];
  for (const [key, expected] of Object.entries(source)) {
    const path = prefix + key;
    if (!(key in translation)) {
      problems.push(`missing key ${path}`);
      continue;
    }
    const actual = translation[key];
    if (kind(expected) !== kind(actual)) {
      problems.push(`type differs at ${path}: expected ${kind(expected)}, got ${kind(actual)}`);
    } else if (kind(expected) === "group") {
      problems.push(...compareCatalogs(expected, actual, `${path}.`));
    } else if (placeholders(expected) !== placeholders(actual)) {
      problems.push(`placeholders differ at ${path}: expected {${placeholders(expected)}}, got {${placeholders(actual)}}`);
    }
  }
  for (const key of Object.keys(translation)) {
    if (!(key in source)) problems.push(`unexpected key ${prefix}${key}`);
  }
  return problems;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const load = (dir, locale) => JSON.parse(readFileSync(`${dir}/${locale}.json`, "utf8"));
  let failed = false;
  for (const { dir, source, translations } of CATALOG_SETS) {
    for (const locale of translations) {
      for (const problem of compareCatalogs(load(dir, source), load(dir, locale))) {
        console.error(`${dir}/${locale}.json: ${problem}`);
        failed = true;
      }
    }
  }
  if (failed) process.exit(1);
  console.log("catalogs: all translations match their English source");
}
