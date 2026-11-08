import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

// ZIF-153 fence. Kills: a gate that misses a Next or Node warning (also mid-line), fails on the allowed
// no-cache line, or passes when the log is missing.
const script = new URL("./fail-on-warnings.sh", import.meta.url).pathname;
const dir = mkdtempSync(join(tmpdir(), "fail-on-warnings-"));

function run(text) {
  const log = join(dir, `${Math.random()}.log`);
  writeFileSync(log, text);
  return spawnSync("bash", [script, log], { encoding: "utf8" }).status;
}

test("a clean log passes", () => {
  assert.equal(run("✓ Compiled successfully\nℹ pass 790\n"), 0);
});

test("Next warning lines and Node runtime warnings fail, also mid-line", () => {
  assert.equal(run(" ⚠ Compiled with warnings\n"), 1);
  assert.equal(run("(node:42) [MODULE_TYPELESS_PACKAGE_JSON] Warning: Module type of x.ts is not specified\n"), 1);
  assert.equal(run("▶ suite(node:42) ExperimentalWarning: something\n"), 1);
});

test("the no-cache line is allowed, but not alongside another warning", () => {
  const noCache = "⚠ No build cache found. Please configure build caching for faster rebuilds.\n";
  assert.equal(run(noCache), 0);
  assert.equal(run(noCache + "⚠ Something else\n"), 1);
});

test("a missing log fails closed", () => {
  assert.equal(spawnSync("bash", [script, join(dir, "missing.log")]).status, 2);
});
