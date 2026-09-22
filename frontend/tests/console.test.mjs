// Logic behind the console shell: role-gated navigation, the current section, dates in the
// business's language, and comparing two sessions before a write.
process.env.TZ = "America/Sao_Paulo";

import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { allowed, dateLocale, navFor, sameSession, sectionOf, todayLabel } from "../lib/console.ts";

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
