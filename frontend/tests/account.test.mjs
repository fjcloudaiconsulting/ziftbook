// Logic behind the account screens: the resend countdown and taking a link's token out of the address bar.
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { clock, RESEND_AFTER_MS, secondsLeft, takeToken } from "../lib/account.ts";

describe("resend countdown", () => {
  const sentAt = 1_000_000;

  test("stays locked for the whole 60 seconds", () => {
    assert.equal(RESEND_AFTER_MS, 60_000);
    assert.equal(secondsLeft(sentAt, sentAt), 60);
    assert.equal(secondsLeft(sentAt, sentAt + 59_001), 1);
    assert.equal(secondsLeft(sentAt, sentAt + 59_999), 1);
  });

  test("unlocks at 60 seconds and stays unlocked", () => {
    assert.equal(secondsLeft(sentAt, sentAt + 60_000), 0);
    assert.equal(secondsLeft(sentAt, sentAt + 3_600_000), 0);
  });

  test("reads as minutes and seconds", () => {
    assert.deepEqual([60, 42, 9, 0].map(clock), ["1:00", "0:42", "0:09", "0:00"]);
  });
});

function fakeWindow(url, stored = {}) {
  const calls = [];
  const place = {
    location: new URL(url),
    history: {
      replaceState(_data, _unused, next) {
        calls.push(["replaceState", next]);
        place.location = new URL(next, place.location);
      },
    },
    sessionStorage: {
      getItem: (key) => stored[key] ?? null,
      setItem: (key, value) => {
        calls.push(["setItem", key]);
        stored[key] = value;
      },
    },
  };
  return { place, calls, stored };
}

describe("taking the token from the link", () => {
  test("removes the fragment from the address bar before handing the token over", () => {
    const { place, calls } = fakeWindow("https://app.example/en/reset-password?x=1#tok-EN_123");

    const token = takeToken(place, "reset");

    assert.equal(token, "tok-EN_123");
    assert.equal(place.location.href, "https://app.example/en/reset-password?x=1");
    assert.deepEqual(calls[0], ["replaceState", "/en/reset-password?x=1"]);
  });

  test("a reload of the cleaned page still has the token", () => {
    const first = fakeWindow("https://app.example/en/reset-password#tok-1");
    takeToken(first.place, "reset");

    const reloaded = fakeWindow("https://app.example/en/reset-password", first.stored);

    assert.equal(takeToken(reloaded.place, "reset"), "tok-1");
    assert.deepEqual(reloaded.calls, [], "nothing to clean");
  });

  test("a newer link replaces the token kept from an older one", () => {
    const { place, stored } = fakeWindow("https://app.example/en/reset-password#new", { reset: "old" });

    assert.equal(takeToken(place, "reset"), "new");
    assert.equal(stored.reset, "new");
  });

  test("no fragment and nothing kept means no token", () => {
    assert.equal(takeToken(fakeWindow("https://app.example/en/reset-password").place, "reset"), null);
  });

  test("works when storage is blocked", () => {
    const { place } = fakeWindow("https://app.example/en/sign-up/complete#tok-2");
    place.sessionStorage = {
      getItem() {
        throw new Error("blocked");
      },
      setItem() {
        throw new Error("blocked");
      },
    };

    assert.equal(takeToken(place, "sign-up"), "tok-2");
    assert.equal(place.location.hash, "");
  });
});
