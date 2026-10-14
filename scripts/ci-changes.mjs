// Which CI jobs a pull request's changed paths can affect (ZIF-31). Prints <job>=true|false lines for $GITHUB_OUTPUT.
// Run: git diff --name-only --no-renames HEAD^1 HEAD | node scripts/ci-changes.mjs
import { readFileSync } from "node:fs";

// Read by no build, test or check. Not CONTRIBUTING.md: check-env-doc reads its env table.
const DOCS = /^(?!CONTRIBUTING\.md$)[^/]+\.md$|^docs\/|^graphify-out\/|^LICENSE$/;

export function affected(paths) {
  const jobs = { backend: false, frontend: false, landing: false, repo: false };
  for (const path of paths) {
    if (DOCS.test(path)) continue;
    // The repo checks scan the whole tree (env names and docs, catalogs, Docker Hub references).
    jobs.repo = true;
    if (path === "backend/openapi.json") jobs.backend = jobs.frontend = true;
    else if (path.startsWith("backend/")) jobs.backend = true;
    else if (path.startsWith("frontend/")) jobs.frontend = true;
    else if (path.startsWith("landing/")) jobs.landing = true;
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
