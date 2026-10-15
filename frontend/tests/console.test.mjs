// Logic behind the console shell: role-gated navigation, the current section, dates in the
// business's language, and comparing two sessions before a write.
process.env.TZ = "America/Sao_Paulo";

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

import {
  allowed,
  archivedDate,
  bookingPageAddress,
  canAnswerRequests,
  canEditHours,
  dateLocale,
  guardedWrite,
  navFor,
  needsName,
  sameSession,
  sectionOf,
  showPublishStep,
  showSetName,
  stickyHeight,
  todayLabel,
  writeOutcome,
} from "../lib/console.ts";

describe("canAnswerRequests (ZIF-143)", () => {
  test("an owner always answers; a worker only while the business allows it", () => {
    assert.equal(canAnswerRequests("owner", false), true);
    assert.equal(canAnswerRequests("worker", true), true);
    assert.equal(canAnswerRequests("worker", false), false);
  });
});

describe("canEditHours", () => {
  test("guard: an owner always may, self or not", () => {
    assert.equal(canEditHours("owner", true, false), true);
    assert.equal(canEditHours("owner", false, false), true);
    assert.equal(canEditHours("owner", false, true), true);
  });

  test("fence: a worker may only edit their own hours, never anyone else's", () => {
    assert.equal(canEditHours("worker", false, true), false, "someone else's hours, even with the setting on");
  });

  test("fence: a worker's own hours still need the setting on", () => {
    assert.equal(canEditHours("worker", true, false), false);
    assert.equal(canEditHours("worker", true, true), true);
  });
});

describe("needsName", () => {
  test("fence: asked only while the API says the name is null, never for another falsy value", () => {
    assert.equal(needsName(null), true);
    assert.equal(needsName(""), false);
  });

  test("guard: not asked once a name is set", () => {
    assert.equal(needsName("Carol Mendes"), false);
  });
});

describe("showSetName", () => {
  test("fence: shown only for null, never any other falsy value", () => {
    assert.equal(showSetName(null), true);
    assert.equal(showSetName(""), false);
  });

  test("guard: not shown once a name is set", () => {
    assert.equal(showSetName("Kim Peters"), false);
  });
});

describe("navFor", () => {
  test("worker nav has none of team, clients, settings or opening-hours", () => {
    const { sidebar, tabs, more } = navFor("worker");
    for (const section of ["team", "clients", "settings", "opening-hours"]) {
      assert.ok(!sidebar.includes(section), `sidebar has ${section}`);
      assert.ok(!tabs.includes(section), `tabs has ${section}`);
      assert.ok(!more.includes(section), `more has ${section}`);
    }
  });

  test("owner tabs are five, ending in more, whose sheet is exactly opening-hours, clients, settings", () => {
    const { tabs, more } = navFor("owner");
    assert.equal(tabs.length, 5);
    assert.equal(tabs.at(-1), "more");
    assert.deepEqual(more, ["opening-hours", "clients", "settings"]);
  });

  test("only the worker has my-hours", () => {
    const owner = navFor("owner");
    const worker = navFor("worker");
    assert.ok(!owner.sidebar.includes("my-hours") && !owner.tabs.includes("my-hours") && !owner.more.includes("my-hours"));
    assert.ok(worker.sidebar.includes("my-hours"));
  });

  test("owner sidebar order", () => {
    assert.deepEqual(navFor("owner").sidebar, ["today", "calendar", "services", "team", "clients", "opening-hours", "settings"]);
  });
});

describe("sectionOf", () => {
  test("a nested route highlights its section", () => {
    assert.equal(sectionOf("/team/abc"), "team");
    assert.equal(sectionOf("/services/new"), "services");
  });

  test("the root is today", () => {
    assert.equal(sectionOf("/"), "today");
  });
});

describe("allowed", () => {
  test("a nested person page is allowed for the owner but not the worker", () => {
    assert.equal(allowed("owner", "/team/abc"), true);
    assert.equal(allowed("worker", "/team/abc"), false);
  });

  test("a worker may see the services list but not its nested pages", () => {
    assert.equal(allowed("worker", "/services"), true);
    assert.equal(allowed("worker", "/services/new"), false);
  });

  test("the root is allowed for both, my-hours is owner-only off limits", () => {
    assert.equal(allowed("owner", "/"), true);
    assert.equal(allowed("worker", "/"), true);
    assert.equal(allowed("owner", "/my-hours"), false);
  });
});

describe("dateLocale", () => {
  test("en reads dates the British way; nl and pt keep their own locale", () => {
    assert.equal(dateLocale("en"), "en-GB");
    assert.equal(dateLocale("nl"), "nl");
    assert.equal(dateLocale("pt"), "pt");
  });
});

describe("todayLabel", () => {
  test("formats in the business time zone, not the server's", () => {
    // 23:30 UTC is already Wednesday 01:30 in Amsterdam, and still Tuesday 20:30 in São Paulo.
    assert.equal(todayLabel(new Date("2026-09-22T23:30:00Z"), "Europe/Amsterdam", "nl"), "woensdag 23 september");
  });

  test("en reads day, day-number, month, with no comma", () => {
    assert.equal(todayLabel(new Date("2026-09-22T10:00:00Z"), "Europe/Amsterdam", "en"), "Tuesday 22 September");
  });
});

describe("archivedDate", () => {
  test("fence: no year in the current year, the year in any other", () => {
    // A "now" years from the real clock: an implementation that reads the clock itself fails.
    const now = new Date("2030-10-08T10:00:00Z");
    assert.equal(archivedDate("2030-08-04T09:00:00Z", now, "Europe/Amsterdam", "en"), "4 August");
    assert.equal(archivedDate("2029-12-12T09:00:00Z", now, "Europe/Amsterdam", "en"), "12 December 2029");
    assert.equal(archivedDate("2030-08-04T09:00:00Z", now, "Europe/Amsterdam", "nl"), "4 augustus");
    assert.equal(archivedDate("2030-08-04T09:00:00Z", now, "Europe/Amsterdam", "pt"), "4 de agosto");
  });

  test("fence: the day and both years are read in the business time zone, not UTC", () => {
    // 23:30 UTC on 31 December is already 1 January in Amsterdam, and so is "now" (00:30): same
    // year there, though UTC (and São Paulo, this process's zone) still says 2025 for the first.
    const now = new Date("2025-12-31T23:30:00Z");
    assert.equal(archivedDate("2025-12-31T23:10:00Z", now, "Europe/Amsterdam", "en"), "1 January");
    // Now in 2026 in Amsterdam, archived in 2025 there: the year shows.
    assert.equal(archivedDate("2025-12-31T22:50:00Z", now, "Europe/Amsterdam", "en"), "31 December 2025");
    // Archived on 1 January in Amsterdam, read in June: same year there, 2025 against 2026 in UTC.
    assert.equal(archivedDate("2025-12-31T23:30:00Z", new Date("2026-06-01T12:00:00Z"), "Europe/Amsterdam", "en"), "1 January");
  });
});

describe("sameSession", () => {
  const base = { user_id: "u1", tenant_id: "t1" };

  test("a different business is a different session", () => {
    assert.equal(sameSession(base, { user_id: "u1", tenant_id: "t2" }), false);
  });

  test("a different person is a different session", () => {
    assert.equal(sameSession(base, { user_id: "u2", tenant_id: "t1" }), false);
  });

  test("the same person in the same business is the same session", () => {
    assert.equal(sameSession(base, { ...base }), true);
  });
});

describe("guardedWrite", () => {
  const current = { user_id: "u1", tenant_id: "t1" };

  test("a session mismatch never sends, and reports mismatch", async () => {
    let sendCalls = 0;
    const result = await guardedWrite(
      async () => ({ status: 200, data: { user_id: "u2", tenant_id: "t1" } }),
      current,
      async () => {
        sendCalls++;
        return { status: 200 };
      },
    );
    assert.equal(sendCalls, 0);
    assert.equal(result.kind, "mismatch");
  });

  test("a match sends exactly once, only after the session read resolves", async () => {
    const order = [];
    const result = await guardedWrite(
      async () => {
        order.push("read");
        return { status: 200, data: current };
      },
      current,
      async () => {
        order.push("send");
        return { status: 204 };
      },
    );
    assert.deepEqual(order, ["read", "send"]);
    assert.equal(result.kind, "sent");
    assert.equal(result.outcome.status, 204);
  });

  test("a signed-out read never sends", async () => {
    let sendCalls = 0;
    const result = await guardedWrite(
      async () => ({ status: 401 }),
      current,
      async () => {
        sendCalls++;
        return { status: 200 };
      },
    );
    assert.equal(sendCalls, 0);
    assert.equal(result.kind, "signedOut");
  });

  test("an unreachable or otherwise broken read never sends", async () => {
    let sendCalls = 0;
    const result = await guardedWrite(
      async () => ({ status: 0 }),
      current,
      async () => {
        sendCalls++;
        return { status: 200 };
      },
    );
    assert.equal(sendCalls, 0);
    assert.equal(result.kind, "failed");
    assert.equal(result.outcome.status, 0);
  });

  test("a 200 with no identity in the body is a failure, never read as the write's own outcome", () => {
    return (async () => {
      let sendCalls = 0;
      const result = await guardedWrite(
        async () => ({ status: 200, data: undefined }),
        current,
        async () => {
          sendCalls++;
          return { status: 200 };
        },
      );
      assert.equal(sendCalls, 0);
      assert.equal(result.kind, "failed");
    })();
  });
});

describe("writeOutcome", () => {
  test("a sent write's own outcome passes through unchanged", () => {
    assert.deepEqual(writeOutcome({ kind: "sent", outcome: { status: 204 } }), { status: 204 });
  });

  test("signedOut maps to 401", () => {
    assert.equal(writeOutcome({ kind: "signedOut" }).status, 401);
  });

  test("a mismatch never reports as success", () => {
    assert.notEqual(writeOutcome({ kind: "mismatch" }).status, 200);
  });

  test("a failed identity check that happened to answer 200 is never read as the write succeeding", () => {
    // guardedWrite's only route to "failed" with a 200 is the check itself answering 200 with no
    // usable body — never a real write response, so it must never look like one here.
    const outcome = writeOutcome({ kind: "failed", outcome: { status: 200 } });
    assert.notEqual(outcome.status, 200);
  });

  test("a failed identity check with a real error status keeps it", () => {
    assert.equal(writeOutcome({ kind: "failed", outcome: { status: 500 } }).status, 500);
  });
});

describe("bookingPageAddress (ZIF-145)", () => {
  test("F8 fence: origin plus slug, no locale segment", () => {
    // Wrong implementation killed: a locale-prefixed address (/nl/nail-bar-carol).
    assert.equal(bookingPageAddress("https://ziftbook.com", "nail-bar-carol"), "https://ziftbook.com/nail-bar-carol");
  });
});

describe("showPublishStep (ZIF-145, G1)", () => {
  test("guard: true only while unpublished, gone once published", () => {
    assert.equal(showPublishStep(false), true);
    assert.equal(showPublishStep(true), false);
  });
});

describe("stickyHeight (ZIF-144)", () => {
  test("fence: publishes the bar's live height on <html> and takes it back when the bar goes", () => {
    // Wrong implementations killed: a height read once at mount (the savebar's hint wraps later),
    // and a leftover value once the bar unmounts (a page without it would keep the padding).
    const props = new Map();
    const observers = [];
    globalThis.document = { documentElement: { style: { setProperty: (k, v) => props.set(k, v), removeProperty: (k) => props.delete(k) } } };
    globalThis.ResizeObserver = class {
      constructor(callback) { this.callback = callback; this.disconnected = false; observers.push(this); }
      observe() { this.callback(); }
      disconnect() { this.disconnected = true; }
    };
    try {
      const bar = { offsetHeight: 183 };
      const cleanup = stickyHeight("--bottombar-height")(bar);
      assert.equal(props.get("--bottombar-height"), "183px");
      bar.offsetHeight = 211;
      observers[0].callback();
      assert.equal(props.get("--bottombar-height"), "211px");
      cleanup();
      assert.equal(props.has("--bottombar-height"), false);
      assert.equal(observers[0].disconnected, true);
    } finally {
      delete globalThis.document;
      delete globalThis.ResizeObserver;
    }
  });

  test("fence: the console's scroll padding reads every height the bars publish", () => {
    // Wrong implementations killed: no scroll padding at all (focus lands under the bars, as before
    // ZIF-144), a property name the CSS and the ref callbacks spell differently, and a callback never
    // attached to its bar. What this can't prove is the browser's scrolling itself; that was checked
    // by tabbing in Chrome at 390x844 and 1280x800.
    const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
    const rule = read("../app/[locale]/_ui/console.module.css").match(/:global\(html\):has\(\.shell\)\s*\{([^}]*)\}/)?.[1] ?? "";
    const [top, bottom] = rule.match(/scroll-padding-block:\s*(calc\([^;]*?\))\s+(calc\([^;]*\));/)?.slice(1) ?? [];
    assert.ok(top && bottom, "html in the console sets scroll-padding-block with a top and a bottom");
    const measured = {};
    for (const path of ["../app/[locale]/_ui/console.tsx", "../app/[locale]/_ui/save-bar.tsx"]) {
      const source = read(path);
      for (const [, ref, name] of source.matchAll(/const (\w+) = stickyHeight\("(--[\w-]+)"\)/g)) {
        // The bar the callback is attached to: the last styles class on the element carrying ref={...}.
        const classes = source.match(new RegExp(`ref=\\{${ref}\\} className=\\{(.*)`))?.[1] ?? "";
        measured[name] = [...classes.matchAll(/styles\.(\w+)/g)].at(-1)?.[1];
      }
    }
    assert.deepEqual(measured, { "--topbar-height": "topbar", "--bottombar-height": "bottomBar", "--savebar-height": "savebarDesktop" });
    assert.match(top, /var\(--topbar-height\b/);
    assert.match(bottom, /var\(--bottombar-height\b/);
    assert.match(bottom, /var\(--savebar-height\b/);
  });
});
