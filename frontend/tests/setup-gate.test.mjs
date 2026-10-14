// First-run redirect (ZIF-70): the decision logic behind proxy.ts, tested without next/server.
import assert from "node:assert/strict";
import { afterEach, describe, test } from "node:test";

import { readNeedsSetup, setupGate, setupRedirect } from "../lib/setup-gate.ts";

const LOCALES = ["en", "nl", "pt"];

describe("setup redirect", () => {
  test("fence: while setup is needed a page request is sent to the setup page", async () => {
    // Wrong implementation killed: a gate that never redirects (always false).
    const gate = setupGate(async () => true);
    assert.equal(await gate(), true);
    assert.equal(setupRedirect("/nl/team", LOCALES), "/nl/setup");
    assert.equal(setupRedirect("/team", LOCALES), "/setup");
  });

  test("fence: the setup page itself is not redirected", () => {
    // Wrong implementation killed: redirecting every path, which loops forever on /en/setup.
    assert.equal(setupRedirect("/en/setup", LOCALES), null);
    assert.equal(setupRedirect("/setup", LOCALES), null);
    assert.equal(setupRedirect("/pt/setup/", LOCALES), null);
  });

  test("fence: /api and /api/x are never sent to setup", () => {
    // Wrong implementation killed: redirecting every non-setup path, /api included.
    assert.equal(setupRedirect("/api", LOCALES), null);
    assert.equal(setupRedirect("/api/setup", LOCALES), null);
    assert.equal(setupRedirect("/apiary", LOCALES), "/setup");
  });

  test("fence: once false is seen it is cached, a second call makes no upstream request", async () => {
    // Wrong implementation killed: a gate that asks every time.
    let calls = 0;
    const gate = setupGate(async () => (++calls, false));
    assert.equal(await gate(), false);
    assert.equal(await gate(), false);
    assert.equal(calls, 1);
  });

  test("fence: true is not cached, so the first admin ends the redirect", async () => {
    // Wrong implementation killed: caching every answer, which redirects forever after setup.
    const answers = [true, true, false];
    const gate = setupGate(async () => answers.shift());
    assert.equal(await gate(), true);
    assert.equal(await gate(), true);
    assert.equal(await gate(), false);
  });

  test("fence: an upstream failure neither redirects nor caches", async () => {
    // Wrong implementation killed: treating a failure as "no setup needed" and caching it (or
    // letting it throw out of the proxy).
    let calls = 0;
    const gate = setupGate(async () => {
      if (++calls === 1) throw new Error("down");
      return true;
    });
    assert.equal(await gate(), false);
    assert.equal(await gate(), true);
    assert.equal(calls, 2);
  });

  test("fence: concurrent requests share one upstream read", async () => {
    // Wrong implementation killed: a gate that starts a read per caller.
    let calls = 0;
    const gate = setupGate(async () => (++calls, await new Promise((r) => setTimeout(() => r(true), 10))));
    assert.deepEqual(await Promise.all([gate(), gate(), gate()]), [true, true, true]);
    assert.equal(calls, 1);
  });
});

describe("readNeedsSetup", () => {
  const real = globalThis.fetch;
  const before = process.env.ZIF_API_URL;
  // Runs even when an assert fails, so later tests never see the stub.
  afterEach(() => {
    globalThis.fetch = real;
    if (before === undefined) delete process.env.ZIF_API_URL;
    else process.env.ZIF_API_URL = before;
  });
  const answer = (status, body) => {
    process.env.ZIF_API_URL = "http://api.test";
    globalThis.fetch = async () => new Response(JSON.stringify(body), { status });
  };
  test("reads the boolean", async () => {
    answer(200, { needs_setup: true });
    assert.equal(await readNeedsSetup(), true);
    answer(200, { needs_setup: false });
    assert.equal(await readNeedsSetup(), false);
  });

  test("fence: a non-200 throws", async () => {
    // Wrong implementation killed: dropping the response.ok check (the body then reads as an answer).
    answer(503, { needs_setup: false });
    await assert.rejects(readNeedsSetup());
  });

  test("fence: a 200 without a boolean needs_setup throws, never reads as false", async () => {
    // Wrong implementation killed: `needs_setup === true`, which turns garbage into a cached false.
    answer(200, {});
    await assert.rejects(readNeedsSetup());
    answer(200, { needs_setup: "no" });
    await assert.rejects(readNeedsSetup());
  });
});
