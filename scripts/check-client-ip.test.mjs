import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

test("every uvicorn CMD in backend/Dockerfile disables proxy headers", () => {
  const cmds = readFileSync("backend/Dockerfile", "utf8").match(/^CMD \["uvicorn".*$/gm) ?? [];
  assert.ok(cmds.length > 0, "no uvicorn CMD found");
  for (const cmd of cmds) assert.match(cmd, /"--no-proxy-headers"/);
});

test("the frontend's fixed address matches the backend's trusted proxy, in both compose files", () => {
  for (const file of ["docker-compose.yaml", "docker-compose-prod.yaml"]) {
    const source = readFileSync(file, "utf8");
    const [, trusted] = source.match(/ZIF_TRUSTED_PROXIES:\s*(\S+)/) ?? [];
    const [, address] = source.match(/ipv4_address:\s*(\S+)/) ?? [];
    assert.equal(address, trusted, file);
  }
});
