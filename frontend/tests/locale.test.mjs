// ZIF-102: which language a page opens in (CONTRIBUTING.md, "Which language a person sees"). The
// owner's rulings: a saved language always wins, on every surface (4); a guest whose browser speaks
// none of ours gets English (5), and there is one language list (routing.locales).
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { forgetPick, pickedHere, recordPick, takeUnsavedNotice } from "../lib/locale.ts";

describe("a language picked with the language button", () => {
  test("fence: a pick that could not be saved is remembered for this tab", () => {
    // Wrong implementation killed: forgetting it, so the saved language takes the page back.
    forgetPick();
    recordPick("pt", false);
    assert.equal(pickedHere(), "pt");
  });

  test("fence: a saved pick leaves nothing behind (the account holds it now)", () => {
    recordPick("pt", false);
    recordPick("nl", true);
    assert.equal(pickedHere(), null);
  });

  test("fence: the page a pick that could not be saved led to says so, once", () => {
    // Wrong implementation killed: a silent failure (the person believes it is saved), or a note
    // on every page after it.
    // Not on the page the pick was made on, before the language has changed.
    forgetPick();
    recordPick("pt", false);
    assert.equal(takeUnsavedNotice("en"), false);
    assert.equal(takeUnsavedNotice("pt"), true);
    assert.equal(takeUnsavedNotice("pt"), false);
    recordPick("nl", true);
    assert.equal(takeUnsavedNotice("nl"), false);
  });

  test("guard: signing out forgets it, so the next person's own language applies", () => {
    recordPick("pt", false);
    forgetPick();
    assert.equal(pickedHere(), null);
  });
});
