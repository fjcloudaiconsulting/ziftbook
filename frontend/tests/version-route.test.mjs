// GET /version.json (ZIF-158): the console's source for the dev version line.
import assert from "node:assert/strict";
import { afterEach, describe, test } from "node:test";

import { dynamic, GET } from "../app/version.json/route.ts";

const saved = { ...process.env };
afterEach(() => {
  for (const key of ["ZIF_SHOW_VERSION", "ZIF_APP_VERSION", "ZIF_APP_REVISION"]) {
    if (saved[key] === undefined) delete process.env[key];
    else process.env[key] = saved[key];
  }
});

describe("GET /version.json", () => {
  // Without it Next may prerender the route at build time, where the switch is unset.
  test("is rendered per request", () => {
    assert.equal(dynamic, "force-dynamic");
  });
  test("404 when the switch is off", async () => {
    delete process.env.ZIF_SHOW_VERSION;
    assert.equal(GET().status, 404);
  });
  test("200, JSON and no-store when on", async () => {
    Object.assign(process.env, { ZIF_SHOW_VERSION: "true", ZIF_APP_VERSION: "0.27.1", ZIF_APP_REVISION: "315a9c5f0e1d" });
    const res = GET();
    assert.equal(res.status, 200);
    assert.equal(res.headers.get("cache-control"), "no-store");
    assert.deepEqual(await res.json(), { version: "0.27.1", revision: "315a9c5" });
  });
});
