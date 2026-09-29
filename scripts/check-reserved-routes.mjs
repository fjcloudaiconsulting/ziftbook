// F8: every top-level route under frontend/app/[locale] must be in migration 0030's reserved-slug
// list, so a future route can never be shadowed by a business slug. Route groups ("(console)") are
// flattened to their children; "_ui" (a private folder) and "[slug]" (a dynamic segment) are
// skipped. Run: node scripts/check-reserved-routes.mjs
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

const APP_LOCALE_DIR = "frontend/app/[locale]";
const MIGRATION = "backend/migrations/versions/0030_tenant_slug.py";

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

/** The RESERVED word list inside migration 0030's source, as plain strings. */
export function reservedWords(migrationSource) {
  const match = /RESERVED = """([\s\S]*?)"""/.exec(migrationSource);
  if (!match) throw new Error("RESERVED list not found in migration 0030");
  return [...match[1].matchAll(/'([^']+)'/g)].map((m) => m[1]);
}

/** Which of `routes` migration 0030 does not reserve. */
export function unreservedRoutes(routes, reserved) {
  const set = new Set(reserved);
  return routes.filter((r) => !set.has(r));
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const routes = topLevelRoutes(APP_LOCALE_DIR);
  const reserved = reservedWords(readFileSync(MIGRATION, "utf8"));
  const missing = unreservedRoutes(routes, reserved);
  if (missing.length > 0) {
    console.error(`reserved routes: ${missing.join(", ")} not in migration 0030's RESERVED list`);
    process.exit(1);
  }
  console.log(`reserved routes: all ${routes.length} top-level routes are reserved`);
}
