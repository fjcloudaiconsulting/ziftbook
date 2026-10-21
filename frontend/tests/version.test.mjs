// The dev-only version line (ZIF-158): shown only when the deployment switch is exactly "true".
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { appVersion } from "../lib/version.ts";

const build = { ZIF_APP_VERSION: "0.27.1", ZIF_APP_REVISION: "315a9c5f0e1d2c3b4a5968778695a4b3c2d1e0f9" };

describe("appVersion", () => {
  test("is null when the switch is unset", () => {
    assert.equal(appVersion(build), null);
  });
  test("is null for any value but 'true'", () => {
    for (const value of ["", "false", "1", "TRUE", "yes"]) assert.equal(appVersion({ ...build, ZIF_SHOW_VERSION: value }), null);
  });
  test("returns the parts when on, with the revision cut to 7 characters", () => {
    assert.deepEqual(appVersion({ ...build, ZIF_SHOW_VERSION: "true" }), { version: "0.27.1", revision: "315a9c5" });
  });
  test("drops a leading v so the display never reads vv0.27.1", () => {
    assert.equal(appVersion({ ...build, ZIF_APP_VERSION: "v0.27.1", ZIF_SHOW_VERSION: "true" })?.version, "0.27.1");
  });
  test("falls back to dev when the image carries no build args", () => {
    assert.deepEqual(appVersion({ ZIF_SHOW_VERSION: "true" }), { version: "dev", revision: "dev" });
  });
});
