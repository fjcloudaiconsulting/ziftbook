// Which CI jobs a pull request's changed paths can affect (ZIF-31). Prints <job>=true|false lines for $GITHUB_OUTPUT.
// Run: git diff --name-only --no-renames HEAD^1 HEAD | node scripts/ci-changes.mjs
import { readFileSync } from "node:fs";

// Read by no build, test or check. Not CONTRIBUTING.md: check-env-doc reads its env table.
const DOCS = /^(?!CONTRIBUTING\.md$)[^/]+\.md$|^docs\/|^graphify-out\/|^LICENSE$/;

// Source files read directly by tests in the other app.
const CROSS_READS = new Set([
  "backend/openapi.json",
  "backend/app/main.py",
  "backend/app/entitlements.py",
  "backend/app/countries.py",
  "frontend/lib/placeholders.ts",
]);

export function affected(paths) {
  const jobs = { backend: false, frontend: false, landing: false, repo: false };
  for (const path of paths) {
    if (DOCS.test(path)) continue;
    // The repo checks scan the whole tree (env names and docs, catalogs, Docker Hub references).
    jobs.repo = true;
    // The backend also scans this route directory to check reserved business slugs.
    if (CROSS_READS.has(path) || /^frontend\/app\/\[locale\](?:\/|$)/.test(path)) jobs.backend = jobs.frontend = true;
    else if (path.startsWith("backend/")) jobs.backend = true;
    else if (path.startsWith("frontend/")) jobs.frontend = true;
    else if (path.startsWith("landing/")) jobs.landing = true;
    // The warning gate of the frontend and landing steps (ZIF-153).
    else if (path === "scripts/fail-on-warnings.sh") jobs.frontend = jobs.landing = true;
    // Workflows, root config (.nvmrc, compose, version.txt, ...) and any path not listed here run everything.
    else if (!/^(scripts|\.githooks)\/|^CONTRIBUTING\.md$/.test(path)) {
      return { backend: true, frontend: true, landing: true, repo: true };
    }
  }
  return jobs;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const paths = readFileSync(0, "utf8").split("\n").filter(Boolean);
  for (const [job, run] of Object.entries(affected(paths))) console.log(`${job}=${run}`);
}
