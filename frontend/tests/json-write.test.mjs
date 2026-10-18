// Every bodyless write the app calls must send JSON_WRITE: the API refuses a write without a JSON
// Content-Type (415, its CSRF defence), and the generated client drops the header when there is no body.
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";

const frontend = new URL("..", import.meta.url).pathname;
const spec = JSON.parse(readFileSync(join(frontend, "../backend/openapi.json"), "utf8"));

// hey-api's names: "admin-sign_out" -> adminSignOut.
const clientName = (operationId) =>
  operationId.split(/[-_]/).map((part, i) => (i ? part[0].toUpperCase() + part.slice(1) : part)).join("");

const bodyless = Object.values(spec.paths).flatMap((methods) =>
  Object.entries(methods)
    .filter(([method, op]) => ["post", "put", "patch", "delete"].includes(method) && !op.requestBody)
    .map(([, op]) => clientName(op.operationId)),
);

function sources(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return sources(path);
    return /\.tsx?$/.test(entry.name) ? [path] : [];
  });
}

// The text between a call's parentheses.
function argsAt(text, open) {
  let depth = 0;
  for (let i = open; i < text.length; i++) {
    if (text[i] === "(") depth++;
    else if (text[i] === ")" && --depth === 0) return text.slice(open + 1, i);
  }
  return text.slice(open + 1);
}

test("the spec still has bodyless writes to check", () => {
  // GUARD: a renamed operationId must not turn the fence below into a check of nothing.
  assert.ok(bodyless.includes("adminSignOut"), bodyless.join(", "));
  assert.ok(bodyless.includes("sessionSignOut"), bodyless.join(", "));
});

test("every call to a bodyless write sends JSON_WRITE", () => {
  // FENCE: adminSignOut() with no argument answered 415 on prod, so admins could not sign out.
  const missing = [];
  let calls = 0;
  for (const file of [...sources(join(frontend, "app")), ...sources(join(frontend, "lib"))]) {
    const text = readFileSync(file, "utf8");
    for (const name of bodyless) {
      for (const match of text.matchAll(new RegExp(`\\b${name}\\(`, "g"))) {
        calls++;
        if (!argsAt(text, match.index + name.length).includes("JSON_WRITE")) {
          missing.push(`${file.slice(frontend.length)}: ${name}(…)`);
        }
      }
    }
  }
  assert.ok(calls > 0, "no calls found: the scan is looking in the wrong place");
  assert.deepEqual(missing, []);
});
