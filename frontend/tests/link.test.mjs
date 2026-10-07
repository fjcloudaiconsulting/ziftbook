// The per-page store of the opened link: every hashchange is a new opening, and a used link stays forgotten.
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { linkStore } from "../lib/link.ts";

// A window that holds one fragment; take() hands it out once, as takeToken does before the next link arrives.
function fakeWindow() {
  const listeners = new Set();
  let fragment = null;
  return {
    take: () => {
      const taken = fragment;
      fragment = null;
      return taken;
    },
    target: {
      addEventListener: (_, fn) => listeners.add(fn),
      removeEventListener: (_, fn) => listeners.delete(fn),
    },
    open(token) {
      fragment = token;
      for (const fn of listeners) fn();
    },
    set(token) {
      fragment = token;
    },
  };
}

describe("link store", () => {
  test("the same token opened again is a new snapshot", () => {
    const win = fakeWindow();
    const store = linkStore(win.take, win.target);
    store.subscribe(() => {});
    win.set("a.1");
    const first = store.snapshot();
    assert.deepEqual(first, { token: "a.1", opened: 0 });
    assert.equal(store.snapshot(), first);
    win.open("a.1");
    const again = store.snapshot();
    assert.notEqual(again, first);
    assert.deepEqual(again, { token: "a.1", opened: 1 });
  });

  test("a forgotten link stays gone until another one is opened", () => {
    const win = fakeWindow();
    const store = linkStore(win.take, win.target);
    store.subscribe(() => {});
    win.set("a.1");
    store.snapshot();
    store.forget();
    assert.equal(store.snapshot(), null);
    assert.equal(store.snapshot(), null);
    win.open("a.1");
    assert.deepEqual(store.snapshot(), { token: "a.1", opened: 1 });
  });
});
