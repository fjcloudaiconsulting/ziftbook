// ZIF-161: the settings page groups its sections into tabs, with one form and one save bar for all of them.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

import { tabKeyTarget } from "../lib/console.ts";
import { dirtyTabs, FIELDS, tabFromQuery, tabOf, TABS, withoutStripe } from "../lib/settings.ts";

const read = (path) => readFileSync(new URL(`../app/[locale]/(console)/${path}`, import.meta.url), "utf8");
const src = read("settings/settings.tsx");
const payments = read("settings/payments.tsx");
const css = readFileSync(new URL("../app/[locale]/_ui/console.module.css", import.meta.url), "utf8");
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
  });

  test("fence: handling the Stripe return drops only ?stripe, never changing the tab", () => {
    // Kills: the payments panel forcing ?tab=payments over a tab the address names, or dropping ?stripe
    // from a bare return so the page falls back to the first tab.
    for (const search of ["?tab=team&stripe=return", "?stripe=return", "?stripe=refresh", "?tab=payments&stripe=return", "?tab=nope&stripe=return"]) {
      const after = withoutStripe(search);
      assert.equal(new URLSearchParams(after).has("stripe"), false, search);
      assert.equal(tabFromQuery(after), tabFromQuery(search), search);
    }
    assert.equal(withoutStripe("?tab=team&stripe=return"), "tab=team");
    assert.equal(withoutStripe("?stripe=return"), "tab=payments");
    assert.match(payments, /url\.search = withoutStripe\(url\.search\);/);
    assert.doesNotMatch(payments, /router\.replace|searchParams\.set\("tab"/);
  });

  test("fence: a pick writes ?tab back to the address, so a reload keeps it", () => {
    // Kills: a tab picked by click or key that a reload forgets.
    assert.match(src, /url\.searchParams\.set\("tab", next\);\s*window\.history\.replaceState\(/);
  });

  test("fence: the tab follows the address on a soft navigation and on Back/Forward", () => {
    // Kills: a tab read once at mount, so the Settings link from ?tab=team, or Back to ?tab=bookings,
    // changes the address but leaves the old tab showing.
    assert.match(src, /const query = useSearchParams\(\)\.toString\(\);/);
    assert.match(src, /useState<Tab>\(\(\) => tabFromQuery\(query\)\)/);
    assert.match(src, /if \(query !== tabQuery\) \{\s*setTabQuery\(query\);\s*setTab\(tabFromQuery\(query\)\);\s*\}/);
    assert.doesNotMatch(src, /tabFromQuery\(window\.location/);
    // useSearchParams needs a Suspense boundary for the static build.
    assert.match(read("settings/page.tsx"), /<Suspense>\s*<Settings \/>\s*<\/Suspense>/);
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
    assert.match(src, /tabRefs\.current\[TABS\[next\]\]\?\.focus\(\{ preventScroll: true \}\);\s*chooseTab\(TABS\[next\]\);/);
  });

  test("fence: every panel is a Tab stop, so one with nothing focusable yet is still reachable", () => {
    // Kills: a panel (Payments while it loads) that keyboard and screen reader users skip past.
    assert.match(src, /hidden: tab !== id, tabIndex: 0 \}/);
  });

  test("fence: the services form's language tabs move by the same rule", () => {
    // Kills: two copies of the arrow/Home/End logic drifting apart.
    const services = read("services/services.tsx");
    assert.match(services, /tabKeyTarget\(event\.key, LOCALES\.indexOf\(loc\), LOCALES\.length\)/);
    assert.doesNotMatch(services, /event\.key === "ArrowRight"/);
  });
});

describe("ZIF-161 the payments tab while it loads", () => {
  test("fence: it says it is loading instead of showing an empty tab", () => {
    // Kills: the old `return null` while the account loads, which left the tab blank.
    assert.doesNotMatch(payments, /if \(!account && !failed\) return null;/);
    assert.match(payments, /\{!account && !failed && \(\s*<p className=\{uiStyles\.hint\} role="status">\s*\{t\("paymentsLoading"\)\}/);
    for (const l of ["en", "nl", "pt"]) assert.ok(messages(l).paymentsLoading, l);
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

describe("ZIF-161 spacing", () => {
  test("fence: the empty publish form takes no gap, and the Booking page keeps its gap to the next section", () => {
    // Kills: the empty form, a flex item of the shell's column, doubling the gap above the tab bar; and the
    // Booking page and Your business sections touching below 64rem now that they share a panel.
    assert.match(src, /<form\s+id=\{publishId\}\s+hidden\b/);
    assert.match(css, /^\.section \+ \.settingsSection \{ margin-top: 1\.25rem; \}/m);
    assert.doesNotMatch(css, /\.sectionBody > form > button/);
  });
});

describe("ZIF-289 the tab bar on a phone", () => {
  test("fence: a chosen tab scrolls into view, by click or key, and a key's focus does not scroll first", () => {
    // Kills: a tab picked by click or arrow key that stays cut off at the edge of the scrolled bar, or a focus
    // scroll that jumps the bar before the (smooth) scrollIntoView runs.
    assert.match(src, /function chooseTab\(next: Tab\) \{\s*pickTab\(next\);\s*tabRefs\.current\[next\]\?\.scrollIntoView\(\{ block: "nearest", inline: "nearest" \}\);/);
    assert.match(src, /onClick=\{\(\) => chooseTab\(id\)\}/);
    // The key path (focus without its own scroll, then chooseTab) is fenced in "ZIF-161 keyboard" above.
    // A field the save refuses opens its tab without scrolling the page back up to the bar.
    assert.match(src, /flushSync\(\(\) => pickTab\(tabOf\(key\)\)\)/);
  });

  test("fence: the bar fades at an edge with more tabs past it, and scrolls instantly under reduced motion", () => {
    // Kills: a bar that cuts a label mid-word with nothing hinting at more tabs; a fade drawn when every tab
    // fits (the scroll timeline is inactive then); a smooth scroll for someone who asked for less motion; a
    // chosen tab that ends under the fade.
    assert.match(css, /@supports \(animation-timeline: scroll\(\)\) \{\s*\.settingsTabs \{[^}]*mask-image: linear-gradient\(to right, transparent, #000 var\(--fade-start\), #000 calc\(100% - var\(--fade-end\)\), transparent\);[^}]*animation: tab-fade linear both;\s*animation-timeline: scroll\(self inline\);/);
    assert.match(css, /@keyframes tab-fade \{\s*0% \{ --fade-start: 0px; --fade-end: var\(--gutter\); \}/);
    assert.match(css, /@media \(prefers-reduced-motion: no-preference\) \{\s*\.settingsTabs \{ scroll-behavior: smooth; \}/);
    assert.match(css, /\.settingsTabs \{[^}]*scroll-padding-inline: var\(--gutter\);/);
  });
});
