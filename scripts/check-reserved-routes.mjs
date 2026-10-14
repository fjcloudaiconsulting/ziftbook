// F8: every top-level route under frontend/app/[locale] must be in the newest reserved-slug list
// (0030's, replaced by later migrations such as 0035), so a future route can never be shadowed by
// a business slug. Route groups ("(console)") are flattened to their children; "_ui" (a private folder) and "[slug]" (a dynamic segment) are
// skipped. Checked by scripts/check-reserved-routes.test.mjs, which node --test "scripts/*.test.mjs"
// already runs against the real tree — no separate CLI entrypoint.
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

/** The route names a directory listing under app/[locale] yields: route groups flattened, private
 * folders (`_*`) and dynamic segments (`[*]`) skipped, files ignored. */
export function topLevelRoutes(appLocaleDir, readdir = readdirSync) {
  const routes = [];
  for (const entry of readdir(appLocaleDir, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const name = entry.name;
    if (name.startsWith("_") || name.startsWith("[")) continue;
    if (name.startsWith("(") && name.endsWith(")")) {
      routes.push(...topLevelRoutes(join(appLocaleDir, name), readdir));
    } else {
      routes.push(name);
    }
  }
  return routes;
}

/** The RESERVED word list inside a migration's source, as plain strings. Anchored to the line
 * start: a migration that restores an older list on downgrade keeps it as OLD_RESERVED. */
export function reservedWords(migrationSource) {
  const match = /^RESERVED = """([\s\S]*?)"""/m.exec(migrationSource);
  if (!match) throw new Error("RESERVED list not found");
  return [...match[1].matchAll(/'([^']+)'/g)].map((m) => m[1]);
}

/** The RESERVED list of the highest-numbered migration (NNNN_*.py) that defines one: the list
 * slug_ok enforces at head. */
export function newestReservedWords(versionsDir, readdir = readdirSync, readFile = readFileSync) {
  const migrations = readdir(versionsDir)
    .filter((name) => /^\d{4}_.*\.py$/.test(name))
    .sort()
    .reverse();
  for (const name of migrations) {
    const source = readFile(join(versionsDir, name), "utf8");
    if (/^RESERVED = """/m.test(source)) return reservedWords(source);
  }
  throw new Error(`no migration in ${versionsDir} defines RESERVED`);
}

/** Which of `routes` the reserved list does not cover. */
export function unreservedRoutes(routes, reserved) {
  const set = new Set(reserved);
  return routes.filter((r) => !set.has(r));
}
