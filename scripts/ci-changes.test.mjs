import assert from "node:assert/strict";
import { test } from "node:test";

import { affected } from "./ci-changes.mjs";

const none = { backend: false, frontend: false, landing: false, repo: false };
const all = { backend: true, frontend: true, landing: true, repo: true };

test("docs-only changes run no job", () => {
  const docs = ["README.md", "CHANGELOG.md", "docs/adr/1.md", "graphify-out/graph.json", "LICENSE"];
  assert.deepEqual(affected(docs), none);
  assert.deepEqual(affected([]), none);
});

test("each app directory runs its own job plus the repo checks", () => {
  assert.deepEqual(affected(["backend/app/bookings.py"]), { ...none, backend: true, repo: true });
  assert.deepEqual(affected(["frontend/app/page.tsx"]), { ...none, frontend: true, repo: true });
  assert.deepEqual(affected(["landing/worker.js"]), { ...none, landing: true, repo: true });
});

test("backend files the frontend tests read run the backend and the frontend", () => {
  for (const path of ["backend/openapi.json", "backend/app/main.py"]) {
    assert.deepEqual(affected([path]), { ...none, backend: true, frontend: true, repo: true }, path);
  }
});

test("docs folders and markdown inside an app directory are that app's input", () => {
  assert.deepEqual(affected(["backend/README.md"]), { ...none, backend: true, repo: true });
  assert.deepEqual(affected(["frontend/docs/x.ts"]), { ...none, frontend: true, repo: true });
});

test("scripts, hooks and CONTRIBUTING.md (env table) run only the repo checks", () => {
  for (const path of ["scripts/check-env-doc.mjs", ".githooks/commit-msg", "CONTRIBUTING.md"]) {
    assert.deepEqual(affected([path]), { ...none, repo: true }, path);
  }
});

test("workflows, root config and unknown paths run everything", () => {
  const paths = [".github/workflows/ci.yml", ".nvmrc", "Makefile", "docker-compose.yaml", "version.txt", "new-dir/x"];
  // git quotes a name with a newline or non-ASCII byte; it matches no rule and runs everything.
  paths.push('"x\\nbackend=false"');
  for (const path of paths) {
    assert.deepEqual(affected(["README.md", path]), all, path);
  }
});

test("a Release PR (changelog, version, manifest) runs everything", () => {
  assert.deepEqual(affected(["CHANGELOG.md", "version.txt", ".release-please-manifest.json"]), all);
});
