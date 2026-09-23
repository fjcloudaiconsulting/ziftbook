// Shared week-editor logic (opening hours, PR 2; working hours, PR 4): the PUT body, pre-submit
// problem checks, and the weekday/city/list formatting the savebar and error banners use.
process.env.TZ = "America/Sao_Paulo";

import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { changedDays, daysSummary, problemList, weekBody, weekdayName, weekProblems, zoneCity } from "../lib/week.ts";

describe("weekProblems", () => {
  test("touching shifts (09:00-12:00, 12:00-15:00) are not an overlap", () => {
    const days = [{ weekday: 1, shifts: [{ start: "09:00", end: "12:00" }, { start: "12:00", end: "15:00" }] }];
    assert.equal(weekProblems(days, null).byDay[1], undefined);
  });

  test("shifts out of order (13:00-18:00 before 09:00-14:00) still overlap", () => {
    const days = [{ weekday: 1, shifts: [{ start: "13:00", end: "18:00" }, { start: "09:00", end: "14:00" }] }];
    assert.equal(weekProblems(days, null).byDay[1], "overlapping_hours");
  });

  test("09:00-09:00 is end_not_after_start", () => {
    const days = [{ weekday: 1, shifts: [{ start: "09:00", end: "09:00" }] }];
    assert.equal(weekProblems(days, null).byDay[1], "end_not_after_start");
  });

  test("an empty start or end is time_required", () => {
    assert.equal(weekProblems([{ weekday: 1, shifts: [{ start: "", end: "10:00" }] }], null).byDay[1], "time_required");
    assert.equal(weekProblems([{ weekday: 1, shifts: [{ start: "09:00", end: "" }] }], null).byDay[1], "time_required");
  });

  test("09:00-15:00 fits a joined 09:00-12:00 + 12:00-15:00 envelope", () => {
    const envelope = [{ weekday: 1, shifts: [{ start: "09:00", end: "12:00" }, { start: "12:00", end: "15:00" }] }];
    const days = [{ weekday: 1, shifts: [{ start: "09:00", end: "15:00" }] }];
    assert.equal(weekProblems(days, envelope).byDay[1], undefined);
  });

  test("a null envelope (never configured) bounds nothing", () => {
    const days = [{ weekday: 1, shifts: [{ start: "00:00", end: "23:59" }] }];
    assert.equal(weekProblems(days, null).byDay[1], undefined);
  });

  test("a configured envelope with no Monday refuses a Monday shift", () => {
    const envelope = [{ weekday: 2, shifts: [{ start: "09:00", end: "17:00" }] }];
    const days = [{ weekday: 1, shifts: [{ start: "09:00", end: "10:00" }] }];
    assert.equal(weekProblems(days, envelope).byDay[1], "outside_opening_hours");
  });

  test("more than 50 shifts is too_many", () => {
    const shifts = Array.from({ length: 51 }, (_, i) => ({ start: `00:${String(i % 60).padStart(2, "0")}`, end: "23:59" }));
    const days = [{ weekday: 1, shifts }];
    assert.equal(weekProblems(days, null).overall, "too_many");
  });

  test("an all-closed week is opening_hours_required", () => {
    const days = [1, 2, 3, 4, 5, 6, 7].map((weekday) => ({ weekday, shifts: [] }));
    assert.equal(weekProblems(days, null).overall, "opening_hours_required");
  });
});

describe("weekBody", () => {
  test("Sunday is weekday 7, sorted after Monday, never getDay()'s 0", () => {
    const days = [
      { weekday: 7, shifts: [{ start: "10:00", end: "12:00" }] },
      { weekday: 1, shifts: [{ start: "09:00", end: "17:00" }] },
    ];
    assert.deepEqual(weekBody(days), [
      { weekday: 1, starts_at: "09:00", ends_at: "17:00" },
      { weekday: 7, starts_at: "10:00", ends_at: "12:00" },
    ]);
  });
});

describe("weekdayName", () => {
  test("1 is Monday in en, even with the process clock in São Paulo", () => {
    assert.equal(weekdayName(1, "en"), "Monday");
  });
});

describe("zoneCity", () => {
  test("Europe/Amsterdam -> Amsterdam, America/Sao_Paulo -> Sao Paulo", () => {
    assert.equal(zoneCity("Europe/Amsterdam"), "Amsterdam");
    assert.equal(zoneCity("America/Sao_Paulo"), "Sao Paulo");
  });
});

describe("problemList", () => {
  test("three phrases in en read with the Oxford comma, unlike dateLocale's en-GB", () => {
    assert.equal(problemList(["a", "b", "c"], "en"), "a, b, and c");
  });
});

describe("daysSummary", () => {
  test("a run of 3+ reads as a range, en-GB (no Oxford comma) for non-runs", () => {
    assert.equal(daysSummary([2, 3, 4, 5, 6], "en"), "Tuesday to Saturday");
    assert.equal(daysSummary([1, 3, 5], "en"), "Monday, Wednesday and Friday");
  });

  test("a run never wraps Sunday to Monday", () => {
    assert.equal(daysSummary([6, 7, 1], "en"), "Monday, Saturday and Sunday");
  });
});

describe("changedDays", () => {
  test("reordering the same shifts within a day is no change", () => {
    const before = [{ weekday: 3, shifts: [{ start: "09:00", end: "12:00" }, { start: "13:00", end: "17:00" }] }];
    const after = [{ weekday: 3, shifts: [{ start: "13:00", end: "17:00" }, { start: "09:00", end: "12:00" }] }];
    assert.deepEqual(changedDays(before, after), []);
  });
});
