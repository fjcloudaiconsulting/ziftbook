// Shared week-editor logic (opening hours; working hours later): the PUT body, pre-submit problem
// checks, and the weekday/city/list formatting the savebar and error banners use.
process.env.TZ = "America/Sao_Paulo";

import assert from "node:assert/strict";
import { describe, test } from "node:test";

import {
  changedDays,
  daysFromShifts,
  daysSummary,
  emptyWeek,
  envelopeFromShifts,
  overlapWindow,
  problemList,
  weekBody,
  weekdayName,
  weekProblems,
  zoneCity,
} from "../lib/week.ts";

describe("emptyWeek", () => {
  test("seven closed days, Monday first", () => {
    const week = emptyWeek();
    assert.deepEqual(
      week.map((d) => d.weekday),
      [1, 2, 3, 4, 5, 6, 7],
    );
    assert.ok(week.every((d) => d.shifts.length === 0));
  });
});

describe("daysFromShifts", () => {
  test("fills all seven weekdays, sorting each day's shifts by start", () => {
    const days = daysFromShifts([
      { weekday: 3, starts_at: "14:00", ends_at: "18:00" },
      { weekday: 3, starts_at: "09:00", ends_at: "12:00" },
    ]);
    assert.deepEqual(
      days.find((d) => d.weekday === 3).shifts,
      [{ start: "09:00", end: "12:00" }, { start: "14:00", end: "18:00" }],
    );
    assert.deepEqual(days.find((d) => d.weekday === 1).shifts, []);
  });
});

describe("envelopeFromShifts", () => {
  test("no rows means unbounded (null), never seven closed days", () => {
    assert.equal(envelopeFromShifts([]), null);
  });

  test("rows become the same per-weekday shape daysFromShifts gives", () => {
    const envelope = envelopeFromShifts([{ weekday: 2, starts_at: "09:00", ends_at: "17:00" }]);
    assert.deepEqual(envelope.find((d) => d.weekday === 2).shifts, [{ start: "09:00", end: "17:00" }]);
  });
});

describe("weekProblems", () => {
  test("touching shifts (09:00-12:00, 12:00-15:00) are not an overlap", () => {
    const days = [{ weekday: 1, shifts: [{ start: "09:00", end: "12:00" }, { start: "12:00", end: "15:00" }] }];
    assert.equal(weekProblems(days, null).byDay[1], undefined);
  });

  test("shifts out of order (13:00-18:00 before 09:00-14:00) still overlap", () => {
    const days = [{ weekday: 1, shifts: [{ start: "13:00", end: "18:00" }, { start: "09:00", end: "14:00" }] }];
    assert.equal(weekProblems(days, null).byDay[1], "overlapping_hours");
  });

  test("three touching shifts given out of array order are not an overlap", () => {
    // Sorted, these three only touch (09-12, 12-15, 15-18); scrambled, an unsorted neighbour
    // check compares the wrong pairs and would flag them as overlapping.
    const days = [
      {
        weekday: 1,
        shifts: [
          { start: "12:00", end: "15:00" },
          { start: "09:00", end: "12:00" },
          { start: "15:00", end: "18:00" },
        ],
      },
    ];
    assert.equal(weekProblems(days, null).byDay[1], undefined);
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

  test("an empty-array envelope ([], never configured) bounds nothing, same as null", () => {
    const days = [{ weekday: 1, shifts: [{ start: "00:00", end: "23:59" }] }];
    assert.equal(weekProblems(days, []).byDay[1], undefined);
  });

  test("a configured envelope with no Monday refuses a Monday shift", () => {
    const envelope = [{ weekday: 2, shifts: [{ start: "09:00", end: "17:00" }] }];
    const days = [{ weekday: 1, shifts: [{ start: "09:00", end: "10:00" }] }];
    assert.equal(weekProblems(days, envelope).byDay[1], "outside_opening_hours");
  });

  test("every shift on a day must fit the envelope, not just one of them", () => {
    const envelope = [{ weekday: 1, shifts: [{ start: "09:00", end: "12:00" }] }];
    const days = [{ weekday: 1, shifts: [{ start: "09:00", end: "10:00" }, { start: "13:00", end: "14:00" }] }];
    assert.equal(weekProblems(days, envelope).byDay[1], "outside_opening_hours");
  });

  test("more than 50 shifts is too_many", () => {
    const shifts = Array.from({ length: 51 }, (_, i) => ({ start: `00:${String(i % 60).padStart(2, "0")}`, end: "23:59" }));
    const days = [{ weekday: 1, shifts }];
    assert.equal(weekProblems(days, null).overall, "too_many");
  });

  test("exactly 50 shifts is fine, the server's own limit; 51 is too_many", () => {
    const fifty = Array.from({ length: 50 }, (_, i) => ({ start: `00:${String(i % 60).padStart(2, "0")}`, end: "23:59" }));
    assert.notEqual(weekProblems([{ weekday: 1, shifts: fifty }], null).overall, "too_many");
    const fiftyOne = Array.from({ length: 51 }, (_, i) => ({ start: `00:${String(i % 60).padStart(2, "0")}`, end: "23:59" }));
    assert.equal(weekProblems([{ weekday: 1, shifts: fiftyOne }], null).overall, "too_many");
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

describe("overlapWindow", () => {
  test("a nested shift's overlap is the inner window (the intersection), not the outer span", () => {
    assert.deepEqual(overlapWindow([{ start: "09:00", end: "18:00" }, { start: "10:00", end: "11:00" }]), { from: "10:00", to: "11:00" });
  });

  test("no overlap is null", () => {
    assert.equal(overlapWindow([{ start: "09:00", end: "12:00" }, { start: "13:00", end: "14:00" }]), null);
  });

  test("the nested shift given first (reversed order) still finds the inner window", () => {
    assert.deepEqual(overlapWindow([{ start: "10:00", end: "11:00" }, { start: "09:00", end: "18:00" }]), { from: "10:00", to: "11:00" });
  });
});

describe("daysSummary", () => {
  const to = (from, to) => `${from} to ${to}`;

  test("a run of 3+ reads as a range, from the given formatter, not hardcoded English", () => {
    assert.equal(daysSummary([2, 3, 4, 5, 6], "en-GB", to), "Tuesday to Saturday");
    assert.equal(
      daysSummary([2, 3, 4, 5, 6], "en-GB", (from, until) => `${from} until ${until}`),
      "Tuesday until Saturday",
    );
  });

  test("en-GB (no Oxford comma) for non-runs", () => {
    assert.equal(daysSummary([1, 3, 5], "en-GB", to), "Monday, Wednesday and Friday");
  });

  test("a run of 2 is a plain list, not a range: a range needs 3 or more", () => {
    assert.equal(daysSummary([2, 3], "en-GB", to), "Tuesday and Wednesday");
  });

  test("a run never wraps Sunday to Monday", () => {
    assert.equal(daysSummary([6, 7, 1], "en-GB", to), "Monday, Saturday and Sunday");
  });
});

describe("changedDays", () => {
  test("reordering the same shifts within a day is no change", () => {
    const before = [{ weekday: 3, shifts: [{ start: "09:00", end: "12:00" }, { start: "13:00", end: "17:00" }] }];
    const after = [{ weekday: 3, shifts: [{ start: "13:00", end: "17:00" }, { start: "09:00", end: "12:00" }] }];
    assert.deepEqual(changedDays(before, after), []);
  });

  test("an identically-ordered week is no change, checked on both sides of the comparison", () => {
    const before = [{ weekday: 3, shifts: [{ start: "13:00", end: "17:00" }, { start: "09:00", end: "12:00" }] }];
    const after = [{ weekday: 3, shifts: [{ start: "13:00", end: "17:00" }, { start: "09:00", end: "12:00" }] }];
    assert.deepEqual(changedDays(before, after), []);
  });
});
