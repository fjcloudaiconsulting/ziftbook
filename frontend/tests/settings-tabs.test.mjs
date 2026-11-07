// ZIF-161: the settings page groups its sections into tabs, with one form and one save bar for all of them.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

import { dirtyTabs, FIELDS, tabFromQuery, tabKeyTarget, tabOf, TABS } from "../lib/settings.ts";

const src = readFileSync(new URL("../app/[locale]/(console)/settings/settings.tsx", import.meta.url), "utf8");
const payments = readFileSync(new URL("../app/[locale]/(console)/settings/payments.tsx", import.meta.url), "utf8");
const messages = (l) => JSON.parse(readFileSync(new URL(`../messages/${l}.json`, import.meta.url), "utf8")).Console.settings;

describe("ZIF-161 the tab from the query string", () => {
  test("fence: ?tab=<id> opens that tab, anything else the first", () => {
    // Kills: a deep link or a reload that always lands on the first tab, or an unknown ?tab= that opens nothing.
    for (const tab of TABS) assert.equal(tabFromQuery(`?tab=${tab}`), tab);
    assert.equal(tabFromQuery(""), "business");
    assert.equal(tabFromQuery("?tab=nope"), "business");
    assert.equal(tabFromQuery("?tab="), "business");
    assert.equal(tabFromQuery("?tab=constructor"), "business");
  });

  test("fence: a return from Stripe lands on the payments tab, with or without ?tab", () => {
    // Kills: an owner back from Stripe's onboarding on the first tab, not seeing the state it came back for
    // (links made before the return URL carried the tab still say only ?stripe=).
    assert.equal(tabFromQuery("?tab=payments&stripe=return"), "payments");
    assert.equal(tabFromQuery("?stripe=return"), "payments");
    assert.equal(tabFromQuery("?stripe=refresh"), "payments");
    assert.match(payments, /router\.replace\(\{ pathname: "\/settings", query: \{ tab: "payments" \} \}\)/);
  });

  test("fence: a pick writes ?tab back to the address, so a reload keeps it", () => {
    // Kills: a tab picked by click or key that a reload forgets.
    assert.match(src, /useState<Tab>\(\(\) => tabFromQuery\(window\.location\.search\)\)/);
    assert.match(src, /url\.searchParams\.set\("tab", next\);\s*window\.history\.replaceState\(/);
  });
});

describe("ZIF-161 keyboard", () => {
  test("fence: arrows move and wrap, Home and End jump, other keys do nothing", () => {
    // Kills: arrows that stop at the ends, or a handler that swallows Tab, Enter or letters.
    const n = TABS.length;
    assert.equal(tabKeyTarget("ArrowRight", 0, n), 1);
    assert.equal(tabKeyTarget("ArrowRight", n - 1, n), 0);
    assert.equal(tabKeyTarget("ArrowLeft", 0, n), n - 1);
    assert.equal(tabKeyTarget("ArrowLeft", 2, n), 1);
    assert.equal(tabKeyTarget("Home", 2, n), 0);
    assert.equal(tabKeyTarget("End", 0, n), n - 1);
    for (const key of ["Tab", "Enter", " ", "a", "ArrowDown"]) assert.equal(tabKeyTarget(key, 1, n), null);
  });

  test("fence: one tab in the Tab order, and a key moves focus with the selection", () => {
    // Kills: every tab a Tab stop, or an arrow that selects a tab while focus stays on the old one.
    assert.match(src, /role="tab"/);
    assert.match(src, /aria-selected=\{tab === id\}/);
    assert.match(src, /tabIndex=\{tab === id \? 0 : -1\}/);
    assert.match(src, /pickTab\(TABS\[next\]\);\s*tabRefs\.current\[TABS\[next\]\]\?\.focus\(\);/);
  });
});

describe("ZIF-161 the unsaved marker", () => {
  test("fence: a tab is marked when it holds a changed field, and only then", () => {
    // Kills: a marker on the wrong tab, on every tab, or one that stays after Undo or a save.
    assert.deepEqual(dirtyTabs([]), []);
    assert.deepEqual(dirtyTabs(["buffer_pct"]), ["bookings"]);
    assert.deepEqual(dirtyTabs(["workers_edit_own_hours", "timezone", "max_reschedules"]), ["business", "bookings", "team"]);
    assert.deepEqual(dirtyTabs(["payment_check"]), ["payments"]);
    assert.match(src, /const marked = dirtyTabs\(changed\);/);
  });

  test("fence: every field of the form sits in a tab", () => {
    // Kills: a new field with no tab, whose change marks nothing and whose refusal opens nothing.
    for (const { key } of FIELDS) assert.ok(TABS.includes(tabOf(key)), key);
  });

  test("fence: the marker has words for a screen reader, in every language", () => {
    // Kills: a dot only sighted owners notice.
    assert.match(src, /<span className=\{styles\.tabDot\} aria-hidden="true" \/>\s*<span className=\{uiStyles\.srOnly\}>/);
    for (const l of ["en", "nl", "pt"]) {
      const m = messages(l);
      assert.ok(m.tabUnsaved, l);
      for (const tab of TABS) assert.ok(m.tabs[tab], `${l} ${tab}`);
    }
  });
});

describe("ZIF-161 switching tabs keeps edits", () => {
  test("fence: every panel stays mounted and is only hidden, inside the one form the save bar submits", () => {
    // Kills: panels rendered only while selected (an edit in a tab left behind is lost, and Save never sends it),
    // or a form per tab (Save sends only the open one).
    assert.match(src, /hidden: tab !== id/);
    for (const tab of TABS) assert.equal(src.split(`<div {...panel("${tab}")}>`).length, 2, tab);
    assert.doesNotMatch(src, /tab === "\w+" &&/);
    const form = src.indexOf("<form id={formId} noValidate onSubmit={onSubmit}>");
    assert.ok(form > 0);
    for (const tab of TABS) assert.ok(src.indexOf(`<div {...panel("${tab}")}>`) > form, tab);
    assert.ok(src.indexOf("<SaveBar") > src.indexOf('<div {...panel("team")}>'));
  });

  test("fence: a refused field opens its tab before it takes focus", () => {
    // Kills: focusing a field in a hidden panel, which does nothing, so the owner never sees why Save failed.
    assert.match(src, /flushSync\(\(\) => pickTab\(tabOf\(key\)\)\);/);
    assert.doesNotMatch(src, /getElementById\(`\$\{formId\}-\$\{(bad|refused)\[0\]\}`\)/);
    assert.match(src, /focusField\(bad\[0\]\);/);
    // ...and with its fieldset enabled again: the save's own `saving` disables it until the finally.
    assert.match(src, /setSaving\(false\);\s*focusField\(refused\[0\]\);/);
  });

  test("fence: the publish switch keeps its own form, not nested in the settings form", () => {
    // Kills: a nested <form> (invalid HTML; the browser drops it), or publish sending the settings form.
    assert.equal(src.match(/<form\b/g).length, 2);
    assert.match(src, /form=\{publishId\}/);
    assert.ok(src.indexOf("<form\n        id={publishId}") < src.indexOf("<form id={formId}"));
  });
});
