// Every ZIF_* variable read in code, defined as a pydantic field (env_prefix="ZIF_" in
// backend/app/config.py) or used in a compose file must appear in the "Environment variables"
// table in CONTRIBUTING.md. Run: node scripts/check-env-doc.mjs
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";

const CONFIG_PY = "backend/app/config.py";
const SCAN_PATHS = ["backend", "frontend", "scripts", "docker-compose.yaml", "docker-compose-prod.yaml", ".github/workflows"];
// The checkers' own tests contain names on purpose, some undocumented, some fake.
const SKIP = new Set(["scripts/check-env-names.test.mjs", "scripts/check-env-doc.test.mjs"]);

export function documentedNames(contributing) {
  return new Set([...contributing.matchAll(/\|\s*`(ZIF_[A-Z0-9_]+)`\s*\|/g)].map((m) => m[1]));
}

// Pydantic settings fields at 4-space indent become ZIF_<FIELD> through env_prefix="ZIF_".
export function pydanticFieldNames(source) {
  return [...source.matchAll(/^ {4}([a-z][a-z0-9_]*):\s/gm)].map((m) => `ZIF_${m[1].toUpperCase()}`);
}

export function usedNames(source) {
  return [...new Set([...source.matchAll(/\bZIF_[A-Z0-9_]+\b/g)].map((m) => m[0]))];
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const documented = documentedNames(readFileSync("CONTRIBUTING.md", "utf8"));

  const found = new Set(pydanticFieldNames(readFileSync(CONFIG_PY, "utf8")));
  const files = execFileSync("git", ["ls-files", ...SCAN_PATHS], { encoding: "utf8" })
    .split("\n")
    // Test files use fake or sentinel ZIF_ names on purpose; they don't need documenting.
    .filter((path) => path && !SKIP.has(path) && !/(^|\/)tests\//.test(path));
  for (const path of files) {
    for (const name of usedNames(readFileSync(path, "utf8"))) found.add(name);
  }

  let failed = false;
  for (const name of [...found].sort()) {
    if (!documented.has(name)) {
      console.error(`${name}: not documented in CONTRIBUTING.md's "Environment variables" table`);
      failed = true;
    }
  }
  if (failed) process.exit(1);
  console.log(`env doc: all ${found.size} ZIF_* names are documented in CONTRIBUTING.md`);
}
