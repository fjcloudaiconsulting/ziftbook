// Every environment variable ziftbook defines starts with ZIF_. Names required by third-party tools
// (NODE_ENV, POSTGRES_*, PG*) are only allowed where listed below. Run: node scripts/check-env-names.mjs
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";

const ALLOWED = new Set(["NODE_ENV"]);
const NAME = "([A-Za-z_][A-Za-z0-9_]*)";

const READERS = {
  ts: [new RegExp(`process\\.env\\.${NAME}`, "g"), new RegExp(`process\\.env\\[\\s*["'\`]${NAME}["'\`]`, "g")],
  py: [
    new RegExp(`os\\.environ\\[\\s*["']${NAME}["']`, "g"),
    new RegExp(`os\\.(?:environ\\.(?:get|setdefault|pop)|getenv)\\(\\s*["']${NAME}["']`, "g"),
    new RegExp(`monkeypatch\\.(?:setenv|delenv)\\(\\s*["']${NAME}["']`, "g"),
  ],
  sql: [new RegExp(`\\\\getenv\\s+\\w+\\s+${NAME}`, "g")],
  dockerfile: [new RegExp(`^ARG\\s+${NAME}`, "gm")],
};

function languageOf(path) {
  if (/\.(ts|tsx|mjs|js)$/.test(path)) return "ts";
  if (path.endsWith(".py")) return "py";
  if (path.endsWith(".sql")) return "sql";
  if (/(^|\/)Dockerfile$/.test(path)) return "dockerfile";
  return null;
}

export function unprefixedNames(path, source) {
  const language = languageOf(path);
  if (!language) return [];
  const found = [];
  for (const pattern of READERS[language]) {
    for (const match of source.matchAll(pattern)) found.push({ index: match.index, name: match[1] });
  }
  const problems = found
    .sort((a, b) => a.index - b.index)
    .map((f) => f.name)
    .filter((name) => !name.startsWith("ZIF_") && !ALLOWED.has(name));
  if (language === "py" && /\(BaseSettings\)/.test(source) && !/env_prefix\s*=\s*["']ZIF_["']/.test(source)) {
    problems.push('BaseSettings without env_prefix="ZIF_"');
  }
  return problems;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const files = execFileSync("git", ["ls-files", "apps", "landing", "scripts"], { encoding: "utf8" })
    .split("\n")
    // The checker's own tests contain unprefixed names on purpose.
    .filter((path) => languageOf(path) && path !== "scripts/check-env-names.test.mjs");
  let failed = false;
  for (const path of files) {
    for (const problem of unprefixedNames(path, readFileSync(path, "utf8"))) {
      console.error(`${path}: ${problem} (environment variables must start with ZIF_)`);
      failed = true;
    }
  }
  if (failed) process.exit(1);
  console.log(`env names: all ${files.length} scanned files use the ZIF_ prefix`);
}
