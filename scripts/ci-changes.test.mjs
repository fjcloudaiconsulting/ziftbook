import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
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
  for (const path of ["backend/openapi.json", "backend/app/main.py", "backend/app/entitlements.py", "backend/app/countries.py"]) {
    assert.deepEqual(affected([path]), { ...none, backend: true, frontend: true, repo: true }, path);
  }
});

test("frontend files and route directories read by backend tests run both apps", () => {
  for (const path of ["frontend/lib/placeholders.ts", "frontend/app/[locale]", "frontend/app/[locale]/new-route/page.tsx", "frontend/app/[locale]/(console)/settings/page.tsx"]) {
    assert.deepEqual(affected([path]), { ...none, backend: true, frontend: true, repo: true }, path);
  }
  assert.deepEqual(affected(["frontend/app/[locale]-other/page.tsx"]), { ...none, frontend: true, repo: true });
});

function crossReads(source, tree) {
  // shortcut: scan literal paths and Path joins; extend if a read constructs the tree name.
  // Join Python Path literals; unresolved root-only literals fail the mapping check too.
  const joined = source.replace(/["']\s*\/\s*["']/g, "/");
  const path = new RegExp(`["'\x60](?:\\.\\./)*(${tree}(?:/[^"'\x60\\r\\n]*)?)["'\x60]`, "g");
  return [...joined.matchAll(path)]
    .filter((match) => match[1].includes("/") || match[0].includes("../")
      || /(?:\/\s*|\b(?:join|resolve|URL|Path)\([^;\n]*)$/.test(joined.slice(0, match.index)))
    .map((match) => match[1]);
}

function assertMapped(path, suite, file) {
  assert.ok(path.includes("/"), `${file}: resolve the computed ${path} path so its CI mapping can be checked`);
  assert.equal(affected([path])[suite], true, `${file} reads ${path}: add its CI mapping`);
}

test("cross-read scanning finds URL, joined and directory paths", () => {
  assert.deepEqual(crossReads('new URL("../../backend/app/new.py", import.meta.url)', "backend"), ["backend/app/new.py"]);
  assert.deepEqual(crossReads('join(root, "../backend/openapi.json")', "backend"), ["backend/openapi.json"]);
  assert.deepEqual(crossReads('API_DIR.parent / "frontend" / "lib" / "placeholders.ts"', "frontend"), ["frontend/lib/placeholders.ts"]);
  assert.deepEqual(crossReads('root / "frontend" / "app" / "[locale]"', "frontend"), ["frontend/app/[locale]"]);
  assert.deepEqual(crossReads('join(root, "backend", variable)', "backend"), ["backend"]);
  assert.deepEqual(crossReads('root / "frontend" / variable', "frontend"), ["frontend"]);
  assert.deepEqual(crossReads('parametrize("trusted", ["*", "frontend", "10.0.0.1"])', "frontend"), []);
  assert.throws(() => assertMapped("backend", "frontend", "new.test.mjs"), /resolve the computed/);
  assert.throws(() => assertMapped("backend/app/new.py", "frontend", "new.test.mjs"), /add its CI mapping/);
});

test("every cross-tree path in app tests triggers the suite that reads it", () => {
  const root = new URL("../", import.meta.url);
  for (const [suite, other, suffix] of [["backend", "frontend", ".py"], ["frontend", "backend", ".mjs"]]) {
    const directory = new URL(`${suite}/tests/`, root);
    const found = new Set();
    for (const file of readdirSync(directory).filter((name) => name.endsWith(suffix))) {
      for (const path of crossReads(readFileSync(new URL(file, directory), "utf8"), other)) {
        found.add(path);
        assertMapped(path, suite, `${suite}/tests/${file}`);
      }
    }
    assert.ok(found.size > 0, `${suite}: cross-read scan found no paths`);
  }
});

// ZIF-153 fence. Kills: a change to the warning gate that skips the jobs using it.
test("the warning gate runs the frontend and landing jobs", () => {
  assert.deepEqual(affected(["scripts/fail-on-warnings.sh"]), { ...none, frontend: true, landing: true, repo: true });
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
