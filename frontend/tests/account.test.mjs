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

function fakeWindow(url) {
  const later = [];
  const place = {
    location: new URL(url),
    history: {
      replaceState(_data, _unused, next) {
        place.location = new URL(next, place.location);
      },
    },
    setTimeout: (callback) => later.push(callback),
  };
  return { place, runLater: () => later.splice(0).forEach((callback) => callback()) };
}

describe("taking the token from the link", () => {
  test("hands the token over and then removes the fragment, keeping the path and query", () => {
    const { place, runLater } = fakeWindow("https://app.example/en/reset-password?x=1#tok-EN_123");

    assert.equal(takeToken(place), "tok-EN_123");
    runLater();

    assert.equal(place.location.href, "https://app.example/en/reset-password?x=1");
  });

  test("leaves the address bar alone until the page's router is ready", () => {
    // Next wraps history.replaceState after hydration; a call made while hydrating must wait for it.
    const { place } = fakeWindow("https://app.example/en/reset-password#tok-1");

    takeToken(place);

    assert.equal(place.location.hash, "#tok-1");
  });

  test("no fragment means no token, and nothing is kept for a reload", () => {
    const first = fakeWindow("https://app.example/en/sign-up/complete#tok-2");
    takeToken(first.place);
    first.runLater();

    assert.equal(takeToken(first.place), null);
    assert.equal(takeToken(fakeWindow("https://app.example/en/sign-up/complete").place), null);
  });
});
