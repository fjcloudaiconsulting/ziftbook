// Checks every translation catalog against its English source: same keys, same {placeholders}.
// Run: node scripts/check-catalogs.mjs
import { readFileSync } from "node:fs";

const CATALOG_SETS = [
  { dir: "frontend/messages", source: "en", translations: ["nl", "pt"] },
  { dir: "landing/strings", source: "en", translations: ["nl", "pt"] },
];

// A real ICU argument ({count}, {count, plural, ...}) has its name immediately followed by `}` or
// `,`. An ICU plural/select branch's message ("=1 {One day needs attention.}") opens a brace with
// its own literal text, which starts the same way but isn't a placeholder: the lookahead tells the
// two apart without a full ICU parser.
// A one-word branch ("one {day}") looks like a placeholder, so branch openers are blanked, but only
// inside a {x, plural|select|selectordinal, ...} block: prose like "Book one {service}" keeps its own.
function blankBranches(text) {
  let out = "";
  let i = 0;
  for (const m of text.matchAll(/\{\w+,\s*(?:plural|select|selectordinal)\s*,/g)) {
    if (m.index < i) continue;
    let depth = 0;
    let end = m.index;
    for (; end < text.length; end++) {
      if (text[end] === "{") depth++;
      else if (text[end] === "}" && --depth === 0) break;
    }
    out += text.slice(i, m.index) + text.slice(m.index, end + 1).replace(/\b(?:zero|one|two|few|many|other|=\d+)\s*\{/g, "[");
    i = end + 1;
  }
  return out + text.slice(i);
}
const placeholders = (text) => [...new Set([...blankBranches(text).matchAll(/\{(\w+)(?=[,}])/g)].map((m) => m[1]))].sort().join(",");
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

/** Every key that appears twice in one object of a JSON text, as a dotted path. Git merges a key
 * added on two branches without a conflict, and JSON.parse keeps the last copy silently. */
export function duplicateKeys(text) {
  const found = [];
  const stack = []; // per open object or array: { keys, path, object, pending }
  let i = 0;
  const readString = () => {
    let end = i + 1;
    while (text[end] !== '"') end += text[end] === "\\" ? 2 : 1;
    const value = JSON.parse(text.slice(i, end + 1));
    i = end + 1;
    return value;
  };
  while (i < text.length) {
    const c = text[i];
    const top = stack[stack.length - 1];
    if (c === '"') {
      const value = readString();
      let j = i;
      while (/\s/.test(text[j] ?? "")) j++;
      if (top?.object && text[j] === ":") {
        const path = top.path ? `${top.path}.${value}` : value;
        if (top.keys.has(value)) found.push(path);
        top.keys.add(value);
        top.pending = path;
      }
      continue;
    }
    if (c === "{" || c === "[") stack.push({ keys: new Set(), path: top?.pending ?? "", object: c === "{", pending: top?.pending ?? "" });
    else if (c === "}" || c === "]") stack.pop();
    i++;
  }
  return found;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const load = (dir, locale) => JSON.parse(readFileSync(`${dir}/${locale}.json`, "utf8"));
  let failed = false;
  for (const { dir, source, translations } of CATALOG_SETS) {
    for (const locale of [source, ...translations]) {
      for (const key of duplicateKeys(readFileSync(`${dir}/${locale}.json`, "utf8"))) {
        console.error(`${dir}/${locale}.json: key ${key} appears twice`);
        failed = true;
      }
    }
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
