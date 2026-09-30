// Pure logic behind Today's requests queue and appointments: the expiry label's day (in the
// business zone, never the browser's), which appointments are past / current / future for the
// "now" line, the decline body, and the today window.
//
// TZ is pinned to Sao Paulo (UTC-3, no DST), away from every zone under test, so a comparison
// made in the runner's own zone reads differently than the business zone instead of matching by luck.
process.env.TZ = "America/Sao_Paulo";

import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { agendaPhase, applyAnswers, badgeText, blockLabel, declineBody, leaveQueue, todayEmpty, expiryLabel, mergeAgenda, nowLineAt, relativeAgo, sortQueue } from "../lib/today.ts";
import { listWindow } from "../lib/time-off.ts";

const AMS = "Europe/Amsterdam";
const NOW = new Date("2026-09-30T12:00:00Z"); // 14:00 on 30 Sep in Amsterdam, 09:00 in Sao Paulo

describe("expiryLabel (F17)", () => {
  test("01:30 on the next Amsterdam day is tomorrow, though the runner's own day is still the 30th", () => {
    assert.deepEqual(expiryLabel("2026-09-30T23:30:00Z", NOW, AMS), { when: "tomorrow", time: "01:30" });
  });

  test("later the same Amsterdam day is today", () => {
    assert.deepEqual(expiryLabel("2026-09-30T15:05:00Z", NOW, AMS), { when: "today", time: "17:05" });
  });

  test("two or more days out reads as a date", () => {
    assert.deepEqual(expiryLabel("2026-10-03T10:00:00Z", NOW, AMS), { when: "later", time: "12:00" });
  });
});

describe("agenda phase and the now line (F18)", () => {
  const at = (h, m = 0) => Date.UTC(2026, 8, 30, h, m);
  const now = at(12);

  test("an appointment in progress is current, not past", () => {
    assert.equal(agendaPhase({ start: at(11, 30), end: at(12, 30) }, now), "current");
  });

  test("an appointment ending exactly now is past; one starting exactly now is current", () => {
    assert.equal(agendaPhase({ start: at(11), end: at(12) }, now), "past");
    assert.equal(agendaPhase({ start: at(12), end: at(13) }, now), "current");
  });

  test("an appointment not yet started is future", () => {
    assert.equal(agendaPhase({ start: at(13), end: at(14) }, now), "future");
  });

  test("the now line sits before the first future item, after an in-progress one", () => {
    const items = [
      { start: at(9), end: at(10) },
      { start: at(11, 30), end: at(12, 30) },
      { start: at(14), end: at(15) },
    ];
    assert.equal(nowLineAt(items, now), 2);
    assert.equal(nowLineAt(items.slice(0, 1), now), 1);
    assert.equal(nowLineAt(items.slice(2), now), 0);
  });
});

describe("declineBody (F19)", () => {
  test("a blank or whitespace-only message is omitted, never sent as an empty string", () => {
    assert.deepEqual(declineBody(""), { status: "declined" });
    assert.deepEqual(declineBody(" \t\r\n "), { status: "declined" });
    assert.equal("message" in declineBody("  "), false);
  });

  test("the message is trimmed, tabs become spaces and carriage returns go", () => {
    assert.deepEqual(declineBody("  Sorry,\tfully booked.\r\nTry Friday?  "), { status: "declined", message: "Sorry, fully booked.\nTry Friday?" });
  });
});

describe("today window (G3)", () => {
  test("a spring-forward day in Amsterdam is 23 hours", () => {
    assert.deepEqual(listWindow(new Date("2026-03-29T10:00:00Z"), AMS, 1), { from: "2026-03-28T23:00:00.000Z", to: "2026-03-29T22:00:00.000Z" });
  });

  test("a fall-back day in Amsterdam is 25 hours", () => {
    assert.deepEqual(listWindow(new Date("2026-10-25T10:00:00Z"), AMS, 1), { from: "2026-10-24T22:00:00.000Z", to: "2026-10-25T23:00:00.000Z" });
  });

  test("an ordinary day is the business's midnight to midnight, not the runner's", () => {
    assert.deepEqual(listWindow(NOW, AMS, 1), { from: "2026-09-29T22:00:00.000Z", to: "2026-09-30T22:00:00.000Z" });
  });
});

describe("sortQueue", () => {
  test("soonest expiry first, id breaks a tie", () => {
    const rows = [
      { id: "c", expires_at: "2026-09-30T13:00:00Z" },
      { id: "b", expires_at: "2026-09-30T12:00:00Z" },
      { id: "a", expires_at: "2026-09-30T12:00:00Z" },
    ];
    assert.deepEqual(sortQueue(rows).map((r) => r.id), ["a", "b", "c"]);
  });
});

describe("applyAnswers", () => {
  const answers = new Map([
    ["x", "confirmed"],
    ["y", "declined"],
    ["z", "stale"],
  ]);

  test("a queue loses every answered row, whatever the answer", () => {
    assert.deepEqual(applyAnswers([{ id: "x" }, { id: "y" }, { id: "z" }, { id: "w" }], answers, "queue").map((r) => r.id), ["w"]);
  });

  test("the agenda confirms an accepted booking and drops a declined one", () => {
    const rows = [
      { id: "x", status: "pending" },
      { id: "y", status: "pending" },
      { id: "z", status: "pending" },
    ];
    assert.deepEqual(applyAnswers(rows, answers, "agenda"), [
      { id: "x", status: "confirmed" },
      { id: "z", status: "pending" },
    ]);
  });
});

describe("mergeAgenda", () => {
  const win = { from: "2026-09-29T22:00:00.000Z", to: "2026-09-30T22:00:00.000Z" };
  const booking = (id, startsAt, endsAt) => ({ id, starts_at: startsAt, ends_at: endsAt });
  const block = (id, memberId, fields) => ({ id, member_id: memberId, starts_at: null, ends_at: null, first_day: null, last_day: null, ...fields });

  test("bookings and blocks interleave by start; a day block is all day", () => {
    const items = mergeAgenda(
      [booking("b2", "2026-09-30T12:00:00Z", "2026-09-30T13:00:00Z"), booking("b1", "2026-09-30T08:00:00Z", "2026-09-30T09:00:00Z")],
      [block("t1", "m1", { first_day: "2026-09-30", last_day: "2026-09-30" }), block("t2", "m1", { starts_at: "2026-09-30T10:00:00Z", ends_at: "2026-09-30T11:00:00Z" })],
      AMS,
      win,
      { role: "owner", memberId: "m1" },
    );
    assert.deepEqual(items.map((i) => i.key), ["t1", "b1", "t2", "b2"]);
    assert.equal(items[0].allDay, true);
    assert.equal(items[0].startedBefore, false);
  });

  test("a block that started before the window is marked, and a worker sees only their own blocks", () => {
    const items = mergeAgenda(
      [],
      [block("t1", "m1", { starts_at: "2026-09-29T20:00:00Z", ends_at: "2026-09-30T07:00:00Z" }), block("t2", "m2", { starts_at: "2026-09-30T10:00:00Z", ends_at: "2026-09-30T11:00:00Z" })],
      AMS,
      win,
      { role: "worker", memberId: "m1" },
    );
    assert.deepEqual(items.map((i) => [i.key, i.startedBefore]), [["t1", true]]);
  });
});

describe("badgeText and relativeAgo", () => {
  test("over 99 reads 99+", () => {
    assert.equal(badgeText(3), "3");
    assert.equal(badgeText(99), "99");
    assert.equal(badgeText(100), "99+");
  });

  test("minutes under an hour, hours under a day, else days", () => {
    const now = new Date("2026-09-30T12:00:00Z");
    assert.deepEqual(relativeAgo("2026-09-30T11:40:00Z", now), { value: -20, unit: "minute" });
    assert.deepEqual(relativeAgo("2026-09-30T10:00:00Z", now), { value: -2, unit: "hour" });
    assert.deepEqual(relativeAgo("2026-09-27T12:00:00Z", now), { value: -3, unit: "day" });
    assert.deepEqual(relativeAgo("2026-09-30T12:00:05Z", now), { value: 0, unit: "minute" });
  });
});

describe("declineBody strips what the API refuses", () => {
  test("zero-width joiner, line separators, tabs and carriage returns; newlines stay", () => {
    assert.deepEqual(declineBody("a\u200Db\u2028c\u2029d\te\rf\ng"), { status: "declined", message: "ab" + "cd e" + "f\ng" });
  });
});

describe("sortQueue compares instants, not strings", () => {
  test("12:00:00Z is before 12:00:00.5Z even though '.' sorts before 'Z'", () => {
    const rows = [
      { id: "a", expires_at: "2026-09-30T12:00:00.5Z" },
      { id: "b", expires_at: "2026-09-30T12:00:00Z" },
    ];
    assert.deepEqual(sortQueue(rows).map((r) => r.id), ["b", "a"]);
  });
});

describe("leaveQueue (stale closure)", () => {
  test("two answers in a row, then a refetch: each works from the list as it is now", () => {
    const answers = new Map();
    let state = [{ id: "a" }, { id: "b" }, { id: "c" }];
    answers.set("a", "confirmed");
    state = leaveQueue(state, answers);
    // a refetch lands meanwhile with a new row n (and still lists a, which the answers filter)
    state = leaveQueue([{ id: "a" }, { id: "b" }, { id: "c" }, { id: "n" }], answers);
    answers.set("b", "declined");
    state = leaveQueue(state, answers);
    assert.deepEqual(state.map((r) => r.id), ["c", "n"]);
    assert.deepEqual(leaveQueue(null, answers), []);
  });
});

describe("todayEmpty", () => {
  test("empty only when both loads worked and both are empty", () => {
    assert.equal(todayEmpty({ queue: [], items: [], failed: false }), true);
    assert.equal(todayEmpty({ queue: [], items: [], failed: true }), false);
    assert.equal(todayEmpty({ queue: null, items: [], failed: false }), false);
    assert.equal(todayEmpty({ queue: [], items: null, failed: false }), false);
    assert.equal(todayEmpty({ queue: [1], items: [], failed: false }), false);
  });
});

describe("multi-day day block and blockLabel", () => {
  const win = { from: "2026-09-29T22:00:00.000Z", to: "2026-09-30T22:00:00.000Z" };
  const min = (iso) => Date.parse(iso);
  const at = (start, end, allDay = false) => ({ start: min(start), end: min(end), allDay });

  test("a day block that started yesterday is flagged startedBefore by mergeAgenda, yet reads All day", () => {
    const [item] = mergeAgenda([], [{ id: "t", member_id: "m1", starts_at: null, ends_at: null, first_day: "2026-09-29", last_day: "2026-10-01" }], AMS, win, { role: "owner", memberId: "m1" });
    assert.equal(item.allDay, true);
    assert.equal(item.startedBefore, true);
    assert.deepEqual(blockLabel(item, win, NOW, AMS), { kind: "allDay" });
  });

  test("timed block: started before and ends today reads until; ends later reads All day", () => {
    assert.deepEqual(blockLabel(at("2026-09-29T20:00:00Z", "2026-09-30T07:00:00Z"), win, NOW, AMS), { kind: "until", time: "09:00" });
    assert.deepEqual(blockLabel(at("2026-09-29T20:00:00Z", "2026-10-01T07:00:00Z"), win, NOW, AMS), { kind: "allDay" });
  });

  test("timed block: inside today shows start and minutes; crossing midnight shows where it ends", () => {
    assert.deepEqual(blockLabel(at("2026-09-30T10:00:00Z", "2026-09-30T11:00:00Z"), win, NOW, AMS), { kind: "timed", time: "12:00", minutes: 60 });
    assert.deepEqual(blockLabel(at("2026-09-30T20:00:00Z", "2026-10-01T07:00:00Z"), win, NOW, AMS), { kind: "from", time: "22:00", endsOn: "tomorrow", endInstant: "2026-10-01T07:00:00.000Z", endTime: "09:00" });
    assert.equal(blockLabel(at("2026-09-30T20:00:00Z", "2026-10-03T07:00:00Z"), win, NOW, AMS).endsOn, "later");
  });
});
