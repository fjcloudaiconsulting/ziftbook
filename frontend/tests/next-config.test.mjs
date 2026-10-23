import assert from "node:assert/strict";
import { test } from "node:test";
import config from "../next.config.ts";

test("the image optimizer is off (ZB-34: no /_next/image, no sharp at runtime)", () => {
  assert.equal(config.images?.unoptimized, true);
});
