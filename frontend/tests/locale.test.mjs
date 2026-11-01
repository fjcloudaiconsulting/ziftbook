// ZIF-102: which language a page opens in (CONTRIBUTING.md, "Which language a person sees"). The
// owner's rulings: a saved language always wins, on every surface (4); a guest whose browser speaks
// none of ours gets English (5), and there is one language list (routing.locales).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

import { browserLocale, forgetPick, isAdminPath, pickedHere, recordPick, resolveLocale, savePickedLanguage, takeUnsavedNotice } from "../lib/locale.ts";

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

const base = { own: null, signedIn: true, surface: "console", browser: [], business: null, url: "en", picked: null };

describe("resolveLocale", () => {
  test("fence: a signed-in viewer's saved language wins over the link on a public page", () => {
    // Wrong implementation killed: URL first on public pages.
    assert.equal(resolveLocale({ ...base, own: "nl", surface: "public", url: "pt", browser: ["pt-BR"] }), "nl");
  });

  test("fence: in the console, a saved language wins over the browser and the business", () => {
    assert.equal(resolveLocale({ ...base, own: "pt", browser: ["nl-NL"], business: "nl", url: "en" }), "pt");
  });

  test("fence: in the console with no saved language, an unsupported browser gets the business's language", () => {
    // Wrong implementation killed: the business before the browser, or the URL before the business.
    assert.equal(resolveLocale({ ...base, browser: ["de-DE", "de"], business: "nl", url: "en" }), "nl");
    assert.equal(resolveLocale({ ...base, browser: ["de-DE", "pt-BR"], business: "nl", url: "en" }), "pt");
  });

  test("fence: a guest gets the URL's language, never the business's", () => {
    // The proxy already sent an unprefixed address from a German browser to English (ruling 5).
    // Wrong implementation killed: the business language for guests.
    assert.equal(resolveLocale({ ...base, signedIn: false, surface: "public", browser: ["de"], business: "nl", url: "en" }), "en");
    assert.equal(resolveLocale({ ...base, signedIn: false, surface: "public", own: "nl", url: "pt" }), "pt");
  });

  test("fence: a signed-in viewer with no saved language keeps the URL's on a public page", () => {
    // Wrong implementation killed: the console's browser rule applied to public pages.
    assert.equal(resolveLocale({ ...base, surface: "public", browser: ["nl"], business: "nl", url: "pt" }), "pt");
  });

  test("fence: a language picked here that could not be saved keeps this page, for now", () => {
    // Wrong implementation killed: sending the person straight back to the saved language they
    // just moved away from, so the button seems to do nothing.
    assert.equal(resolveLocale({ ...base, own: "nl", url: "pt", picked: "pt" }), "pt");
  });

  test("guard: with nothing to go on, the console keeps the URL's", () => {
    assert.equal(resolveLocale({ ...base, browser: ["de"], business: null, url: "pt" }), "pt");
  });

  test("guard: a value outside the list is never an answer", () => {
    assert.equal(resolveLocale({ ...base, own: "de", browser: ["fr"], business: "de", url: "nl" }), "nl");
  });
});

describe("browserLocale", () => {
  test("fence: the first of the browser's languages we speak, matched on its primary subtag", () => {
    assert.equal(browserLocale(["de-AT", "pt-PT", "nl"]), "pt");
    assert.equal(browserLocale(["NL-be"]), "nl");
    assert.equal(browserLocale(["en-GB"]), "en");
  });

  test("guard: none of ours, none at all", () => {
    assert.equal(browserLocale(["de", "fr-FR"]), null);
    assert.equal(browserLocale([]), null);
  });
});

describe("saving a public page's language pick (savePickedLanguage)", () => {
  const ana = { user_id: "ana", tenant_id: "salon" };

  test("fence: a session that changed to someone else in another tab is never written", async () => {
    // Wrong implementation killed: the PUT sent straight away, which saves the pick to whoever's
    // cookie the browser now holds (Bruno), while telling the clicker it was saved.
    let put = 0;
    const result = await savePickedLanguage(ana, async () => ({ status: 200, data: { user_id: "bruno", tenant_id: "salon" } }), async () => {
      put += 1;
      return { status: 200 };
    });
    assert.equal(result, "mismatch");
    assert.equal(put, 0);
  });

  test("guard: the same person is saved; a signed-out session and a failed save are not", async () => {
    const same = async () => ({ status: 200, data: ana });
    assert.equal(await savePickedLanguage(ana, same, async () => ({ status: 200 })), "saved");
    assert.equal(await savePickedLanguage(ana, same, async () => ({ status: 503 })), "failed");
    assert.equal(await savePickedLanguage(ana, async () => ({ status: 401 }), async () => ({ status: 200 })), "signedOut");
  });
});

describe("platform admin pages (isAdminPath)", () => {
  test("fence: the admin pages are told apart, so they never read or write a business person's language", () => {
    // Wrong implementation killed: the member session read on /admin, which moves the admin console
    // to a business person's language and saves the admin's pick to that person.
    assert.equal(isAdminPath("/admin"), true);
    assert.equal(isAdminPath("/admin/sign-in"), true);
    assert.equal(isAdminPath("/administrator-salon"), false); // a business's slug, not the admin
    assert.equal(isAdminPath("/sign-in"), false);
  });

  test("guard: the public language hook skips its viewer logic there", () => {
    const src = readFileSync(new URL("../app/[locale]/_ui/header.tsx", import.meta.url), "utf8");
    assert.match(src, /const admin = isAdminPath\(pathname\);[\s\S]*if \(admin\) return;[\s\S]*if \(admin \|\| !viewer\) return undefined;/);
  });
});
