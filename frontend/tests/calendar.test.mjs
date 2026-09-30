// Calendar's pure logic: Monday weeks, day windows that follow DST, placement in wall-clock minutes of
// the business zone, lane packing, the visible hour range, which actions a booking offers, the history
// lines and the URL state.
//
// No TZ is pinned here on purpose: the suite must pass in whatever zone the runner is in
// (`TZ=America/New_York node --test tests/calendar.test.mjs`, `TZ=Asia/Tokyo ...`).
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { actionsFor, closedDay, closedDetail, isWaiting, spanTimes, telHref, daySlices, dayWindow, historyLabel, hourRange, hrefFor, lanes, nowTop, parseView, visibleDays, weekStart, weekdayOf, whenParts } from "../lib/calendar.ts";

const AMS = "Europe/Amsterdam";
const at = (iso) => new Date(iso).getTime();

describe("weekStart (F1)", () => {
  test("a Sunday belongs to the week that began the Monday before", () => {
    assert.equal(weekStart("2026-10-04"), "2026-09-28");
  });
  test("a Monday is its own week start, a Wednesday goes back to Monday", () => {
    assert.equal(weekStart("2026-09-28"), "2026-09-28");
    assert.equal(weekStart("2026-09-30"), "2026-09-28");
  });
  test("weekdayOf counts Monday as 1 and Sunday as 7", () => {
    assert.equal(weekdayOf("2026-09-28"), 1);
    assert.equal(weekdayOf("2026-10-04"), 7);
  });
});

describe("dayWindow (F2)", () => {
  test("the day the clocks go back is 25 hours and ends at local midnight", () => {
    const w = dayWindow("2026-10-25", AMS, 1);
    assert.equal(w.to, "2026-10-25T23:00:00.000Z");
    assert.equal((at(w.to) - at(w.from)) / 3_600_000, 25);
  });
  test("the day the clocks go forward is 23 hours", () => {
    const w = dayWindow("2026-03-29", AMS, 1);
    assert.equal((at(w.to) - at(w.from)) / 3_600_000, 23);
  });
  test("a week ends at the local midnight after its seventh day", () => {
    assert.equal(dayWindow("2026-10-19", AMS, 7).to, "2026-10-25T23:00:00.000Z");
  });
});

describe("daySlices (F3, F4)", () => {
  test("10:00 in Amsterdam places at minute 600 on both DST-change days", () => {
    for (const [day, iso] of [
      ["2026-03-29", "2026-03-29T08:00:00Z"],
      ["2026-10-25", "2026-10-25T09:00:00Z"],
    ]) {
      const [slice] = daySlices({ start: at(iso), end: at(iso) + 3_600_000 }, [day], AMS);
      assert.equal(slice.start, 600, day);
      assert.equal(slice.end, 660, day);
    }
  });

  test("a booking over midnight is cut into both days", () => {
    const item = { start: at("2026-09-30T21:00:00Z"), end: at("2026-09-30T23:00:00Z") }; // 23:00 - 01:00 Amsterdam
    const slices = daySlices(item, ["2026-09-30", "2026-10-01"], AMS);
    assert.deepEqual(slices.map((s) => [s.day, s.start, s.end]), [
      ["2026-09-30", 1380, 1440],
      ["2026-10-01", 0, 60],
    ]);
  });

  test("a block from yesterday 22:00 starts at the top of today, never above it", () => {
    const item = { start: at("2026-09-29T20:00:00Z"), end: at("2026-09-30T00:00:00Z") }; // 22:00 - 02:00
    const slices = daySlices(item, ["2026-09-30"], AMS);
    assert.deepEqual(slices.map((s) => [s.start, s.end]), [[0, 120]]);
  });

  test("an item on another day has no slice", () => {
    const item = { start: at("2026-09-28T10:00:00Z"), end: at("2026-09-28T11:00:00Z") };
    assert.deepEqual(daySlices(item, ["2026-09-30"], AMS), []);
  });
});

describe("lanes (F5)", () => {
  const span = (from, to) => ({ start: from * 60, end: to * 60 });
  test("overlapping bookings share the width, a later one is full width again", () => {
    const placed = lanes([span(10, 11), { start: 630, end: 690 }, span(11, 12), span(13, 14)]);
    assert.deepEqual(placed, [
      { lane: 0, of: 2 },
      { lane: 1, of: 2 },
      { lane: 0, of: 2 },
      { lane: 0, of: 1 },
    ]);
  });
  test("one ending when the next starts do not overlap", () => {
    assert.deepEqual(lanes([span(9, 10), span(10, 11)]), [
      { lane: 0, of: 1 },
      { lane: 0, of: 1 },
    ]);
  });
  test("a clustered 10-11, 10-11 and a following 11-12: the third is full width again", () => {
    assert.deepEqual(lanes([span(10, 11), span(10, 11), span(11, 12)]), [
      { lane: 0, of: 2 },
      { lane: 1, of: 2 },
      { lane: 0, of: 1 },
    ]);
  });
  test("two back-to-back 15-minute bookings are drawn 23 minutes tall and so take lanes", () => {
    const slices = [at("2026-09-30T08:00:00Z"), at("2026-09-30T08:15:00Z")].flatMap((start) => daySlices({ start, end: start + 15 * 60_000 }, ["2026-09-30"], AMS));
    assert.deepEqual(slices.map((s) => s.end - s.start), [23, 23]);
    assert.deepEqual(lanes(slices), [
      { lane: 0, of: 2 },
      { lane: 1, of: 2 },
    ]);
  });
  test("02:15 before and after the clocks go back are two bookings side by side", () => {
    const first = at("2026-10-25T00:15:00Z"); // 02:15 CEST
    const second = at("2026-10-25T01:15:00Z"); // 02:15 CET
    const slices = [first, second].flatMap((start) => daySlices({ start, end: start + 3_600_000 }, ["2026-10-25"], AMS));
    assert.deepEqual(lanes(slices).map((l) => l.lane).sort(), [0, 1]);
  });
});

describe("hourRange (F6)", () => {
  const open = [{ weekday: 3, starts_at: "09:00", ends_at: "17:30" }];
  test("opening hours set the range, rounded outwards", () => {
    assert.deepEqual(hourRange(open, [3], []), { from: 9, to: 18 });
  });
  test("a walk-in after closing widens it", () => {
    assert.deepEqual(hourRange(open, [3], [{ start: 1140, end: 1170 }]), { from: 9, to: 20 });
  });
  test("an all-day block does not widen it", () => {
    assert.deepEqual(hourRange(open, [3], [{ start: 0, end: 1440, allDay: true }]), { from: 9, to: 18 });
  });
  test("nothing at all is 09 to 18", () => {
    assert.deepEqual(hourRange([], [3], []), { from: 9, to: 18 });
  });
  test("a closed weekday is 09 to 18 even when another day opens late, a 14:00 walk-in stays inside", () => {
    assert.deepEqual(hourRange([{ weekday: 3, starts_at: "06:00", ends_at: "22:00" }], [7], [{ start: 840, end: 900 }]), { from: 9, to: 18 });
  });
});

describe("actionsFor (F7)", () => {
  const now = new Date("2026-09-30T12:00:00Z");
  const base = { expired: false, starts_at: "2026-09-30T10:00:00Z" };
  test("a lapsed pending hold offers nothing", () => {
    assert.deepEqual(actionsFor({ ...base, status: "pending", expired: true }, "owner", now), []);
  });
  test("a live pending offers accept and decline", () => {
    assert.deepEqual(actionsFor({ ...base, status: "pending" }, "worker", now), ["accept", "decline"]);
  });
  test("confirmed: completed and no-show open at the start, not a minute before", () => {
    assert.deepEqual(actionsFor({ ...base, status: "confirmed", starts_at: "2026-09-30T12:00:00Z" }, "owner", now), ["completed", "no_show", "reschedule", "cancel"]);
    assert.deepEqual(actionsFor({ ...base, status: "confirmed", starts_at: "2026-09-30T12:01:00Z" }, "owner", now), ["reschedule", "cancel"]);
  });
  test("only a confirmed booking can be rescheduled (a pending one is accepted first, D7)", () => {
    for (const status of ["pending", "completed", "no_show", "awaiting_payment"]) {
      assert.ok(!actionsFor({ ...base, status }, "owner", now).includes("reschedule"), status);
    }
  });
  test("completed is the owner's to undo, a worker gets nothing", () => {
    assert.deepEqual(actionsFor({ ...base, status: "completed" }, "worker", now), []);
    assert.deepEqual(actionsFor({ ...base, status: "completed" }, "owner", now), ["restore", "no_show", "cancel"]);
  });
  test("no-show and awaiting payment offer nothing", () => {
    assert.deepEqual(actionsFor({ ...base, status: "no_show" }, "owner", now), []);
    assert.deepEqual(actionsFor({ ...base, status: "awaiting_payment" }, "owner", now), []);
  });
});

describe("historyLabel (F8)", () => {
  const ev = (event, extra = {}) => ({ event, at: "2026-09-28T16:40:00Z", actor: "team", actor_name: "Carol", details: null, ...extra });
  const label = (events, i, source = "booking_page") => historyLabel(events, i, source, 2, AMS);

  test("created reads by source", () => {
    assert.equal(label([ev("created")], 0).key, "historyRequested");
    assert.equal(label([ev("created")], 0, "merchant").key, "historyAdded");
  });
  test("confirmed after completed is an undo, otherwise an accept", () => {
    assert.equal(label([ev("created"), ev("confirmed")], 1).key, "historyAccepted");
    assert.equal(label([ev("created"), ev("confirmed"), ev("completed"), ev("confirmed")], 3).key, "historyUndone");
  });
  test("an undo is read against the previous status event, not the previous event", () => {
    const events = [ev("created"), ev("confirmed"), ev("completed"), ev("consent_confirmed"), ev("confirmed")];
    assert.equal(label(events, 4).key, "historyUndone");
  });
  test("rescheduled shows the business zone times and which change it is", () => {
    const moved = (from, to) => ev("rescheduled", { actor: "client", actor_name: null, details: { from, to } });
    const events = [ev("created"), moved("2026-09-29T11:00:00Z", "2026-09-29T12:00:00Z"), moved("2026-09-30T22:30:00Z", "2026-10-01T08:00:00Z")];
    const first = label(events, 1);
    assert.equal(first.key, "historyMoved");
    assert.deepEqual([first.params.fromTime, first.params.toTime, first.params.sameDay, first.params.k, first.params.max], ["13:00", "14:00", true, 1, 2]);
    const second = label(events, 2);
    assert.deepEqual([second.params.fromTime, second.params.fromDate, second.params.toDate, second.params.sameDay, second.params.k], ["00:30", "2026-10-01", "2026-10-01", true, 2]);
  });
  test("a move by the team reads Moved by {actor}, with no k of max", () => {
    const team = ev("rescheduled", { actor: "team", actor_name: "Carol", details: { from: "2026-09-29T11:00:00Z", to: "2026-09-29T12:00:00Z" } });
    const l = label([ev("created"), team], 1);
    assert.equal(l.key, "historyMovedTeam");
    assert.deepEqual([l.params.actor, l.params.fromTime, l.params.toTime, "k" in l.params], ["Carol", "13:00", "14:00", false]);
  });
  test("k counts only the client's own moves, in order", () => {
    const d = { from: "2026-09-29T11:00:00Z", to: "2026-09-29T12:00:00Z" };
    const events = [
      ev("created"),
      ev("rescheduled", { actor: "team", details: d }),
      ev("rescheduled", { actor: "client", actor_name: null, details: d }),
      ev("rescheduled", { actor: "team", details: d }),
      ev("rescheduled", { actor: "client", actor_name: null, details: d }),
    ];
    assert.equal(label(events, 2).params.k, 1);
    assert.equal(label(events, 4).params.k, 2);
  });
  test("a move across days says so", () => {
    const moved = ev("rescheduled", { details: { from: "2026-09-29T11:00:00Z", to: "2026-09-30T12:00:00Z" } });
    assert.equal(label([moved], 0).params.sameDay, false);
  });
  test("no actor name falls back to null for the caller's team wording", () => {
    assert.equal(label([ev("created"), ev("declined", { actor_name: null })], 1).params.actor, null);
  });
  test("an unknown event, or a move without details, is a plain update and never throws", () => {
    assert.equal(label([ev("teleported")], 0).key, "historyUpdated");
    assert.equal(label([ev("rescheduled")], 0).key, "historyUpdated");
  });
});

describe("parseView (F9)", () => {
  const TODAY = "2026-09-30";
  const parse = (query, role = "owner", members) => parseView(new URLSearchParams(query), TODAY, role, "me", members);
  test("a worker is always themselves", () => {
    assert.equal(parse("member=other", "worker").member, "me");
    assert.equal(parse("view=week", "worker").member, "me");
  });
  test("an impossible date is today", () => {
    assert.equal(parse("date=2026-02-30").date, TODAY);
    assert.equal(parse("date=nonsense").date, TODAY);
    assert.equal(parse("date=2026-10-04").date, "2026-10-04");
  });
  test("an unknown view is the day view", () => {
    assert.equal(parse("view=year").view, "day");
    assert.equal(parse("view=week").view, "week");
  });
  test("an owner defaults to everyone, in the week view too", () => {
    assert.equal(parse("").member, "all");
    assert.equal(parse("view=week").member, "all");
    assert.equal(parse("view=week&member=all").member, "all");
  });
  test("an owner's member id is checked once the members are known", () => {
    assert.equal(parse("member=abc", "owner", ["abc", "def"]).member, "abc");
    assert.equal(parse("member=ghost", "owner", ["abc"]).member, "all");
    assert.equal(parse("member=ghost", "owner", null).member, "ghost");
  });
  test("booking passes through when it is a UUID, anything else or absent is null", () => {
    const id = "0b6f3c1e-8a4d-4f1e-9c2b-5d7e8f9a0b1c";
    assert.equal(parse(`booking=${id}`).booking, id);
    assert.equal(parse("booking=b1").booking, null);
    assert.equal(parse("booking=").booking, null);
    assert.equal(parse("").booking, null);
  });
});

describe("parseView panel state (F24)", () => {
  const TODAY = "2026-09-30";
  const parse = (query, role = "owner", members) => parseView(new URLSearchParams(query), TODAY, role, "me", members);
  const AT = "2026-09-30T13:30:00.000Z";
  test("new and block carry at and with, and drop the open booking", () => {
    const v = parse(`panel=new&booking=0b6f3c1e-8a4d-4f1e-9c2b-5d7e8f9a0b1c&at=${AT}&with=abc`, "owner", ["abc"]);
    assert.deepEqual([v.panel, v.booking, v.at, v.with], ["new", null, AT, "abc"]);
    assert.equal(parse(`panel=block&booking=0b6f3c1e-8a4d-4f1e-9c2b-5d7e8f9a0b1c`).booking, null);
  });
  test("move without a booking is no panel at all", () => {
    assert.equal(parse("panel=move").panel, null);
    assert.equal(parse("panel=move&booking=0b6f3c1e-8a4d-4f1e-9c2b-5d7e8f9a0b1c").panel, "move");
    assert.equal(parse("panel=move&booking=0b6f3c1e-8a4d-4f1e-9c2b-5d7e8f9a0b1c").booking, "0b6f3c1e-8a4d-4f1e-9c2b-5d7e8f9a0b1c");
  });
  test("an unknown panel is none", () => {
    assert.equal(parse("panel=delete").panel, null);
  });
  test("a worker's with is always themselves, even with none given", () => {
    assert.equal(parse("panel=new&with=other", "worker").with, "me");
    assert.equal(parse("panel=block", "worker").with, "me");
  });
  test("an owner's with must be one of the members once they are known", () => {
    assert.equal(parse("panel=new&with=ghost", "owner", ["abc"]).with, null);
    assert.equal(parse("panel=new&with=abc", "owner", ["abc"]).with, "abc");
  });
  test("at must be a real instant, else it is dropped and never throws", () => {
    for (const bad of ["nonsense", "2026-13-45T10:00:00Z", "", "2026-09-30", "99999999999999999-01-01T00:00:00Z"]) {
      assert.equal(parse(`panel=new&at=${encodeURIComponent(bad)}`).at, null, bad);
    }
    assert.equal(parse("panel=new&at=2026-09-30T15:30:00%2B02:00").at, AT);
  });
  test("without a panel, at and with are nothing", () => {
    const v = parse(`at=${AT}&with=abc`);
    assert.deepEqual([v.panel, v.at, v.with], [null, null, null]);
  });
});

describe("nowTop (F10)", () => {
  const now = new Date("2026-09-30T12:00:00Z"); // 14:00 in Amsterdam
  const range = { from: 9, to: 18 };
  test("minutes from the top of the range when today is shown", () => {
    assert.equal(nowTop(now, ["2026-09-30"], range, AMS), 300);
    assert.equal(nowTop(now, ["2026-09-28", "2026-09-29", "2026-09-30"], range, AMS), 300);
  });
  test("nothing when today is not shown", () => {
    assert.equal(nowTop(now, ["2026-10-01"], range, AMS), null);
  });
  test("nothing when now is outside the range", () => {
    assert.equal(nowTop(now, ["2026-09-30"], { from: 9, to: 14 }, AMS), null);
    assert.equal(nowTop(now, ["2026-09-30"], { from: 15, to: 18 }, AMS), null);
  });
});

describe("closedDay (F11)", () => {
  test("never configured is not closed", () => {
    assert.equal(closedDay([], 7), false);
  });
  test("a configured week without the weekday is closed on it", () => {
    const week = [{ weekday: 1, starts_at: "09:00", ends_at: "17:00" }];
    assert.equal(closedDay(week, 7), true);
    assert.equal(closedDay(week, 1), false);
  });
});

describe("hrefFor (G1)", () => {
  const current = { view: "day", date: "2026-09-30", member: "all", booking: "b1" };
  const query = (href) => Object.fromEntries(new URL(href, "http://x").searchParams);
  test("a new date, view or member drops the open booking and keeps the rest", () => {
    assert.deepEqual(query(hrefFor(current, { date: "2026-10-01" })), { view: "day", date: "2026-10-01", member: "all" });
    assert.deepEqual(query(hrefFor(current, { view: "week" })), { view: "week", date: "2026-09-30", member: "all" });
    assert.deepEqual(query(hrefFor(current, { member: "m2" })), { view: "day", date: "2026-09-30", member: "m2" });
  });
  test("opening or closing a booking keeps the window", () => {
    assert.deepEqual(query(hrefFor(current, { booking: "b2" })), { ...current, booking: "b2" });
    assert.deepEqual(query(hrefFor(current, { booking: null })), { view: "day", date: "2026-09-30", member: "all" });
  });
  test("a patch that changes nothing keeps the booking", () => {
    assert.equal(query(hrefFor(current, { date: "2026-09-30" })).booking, "b1");
  });
  test("moving the window drops panel, at and with", () => {
    const open = { view: "day", date: "2026-09-30", member: "all", booking: null, panel: "new", at: "2026-09-30T13:30:00.000Z", with: "m1" };
    for (const patch of [{ date: "2026-10-01" }, { view: "week" }, { member: "m2" }]) {
      const q = query(hrefFor(open, patch));
      assert.deepEqual([q.panel, q.at, q.with], [undefined, undefined, undefined], JSON.stringify(patch));
    }
  });
  test("opening a panel carries its prefill; new drops the booking, move keeps it", () => {
    const q = query(hrefFor(current, { panel: "new", at: "2026-09-30T13:30:00.000Z", with: "m1" }));
    assert.deepEqual([q.panel, q.at, q.with, q.booking], ["new", "2026-09-30T13:30:00.000Z", "m1", undefined]);
    assert.deepEqual([query(hrefFor(current, { panel: "move" })).panel, query(hrefFor(current, { panel: "move" })).booking], ["move", "b1"]);
  });
  test("closing the panel leaves the window and the booking", () => {
    const open = { view: "day", date: "2026-09-30", member: "all", booking: "b1", panel: "move", at: null, with: null };
    assert.deepEqual(query(hrefFor(open, { panel: null })), { view: "day", date: "2026-09-30", member: "all", booking: "b1" });
  });
  test("a finished booking moves to its day and opens its detail, panel gone", () => {
    const open = { view: "day", date: "2026-09-30", member: "all", booking: null, panel: "new", at: null, with: null };
    assert.deepEqual(query(hrefFor(open, { date: "2026-10-02", panel: null, booking: "b9" })), { view: "day", date: "2026-10-02", member: "all", booking: "b9" });
  });
  test("it points at the calendar", () => {
    assert.ok(hrefFor(current, {}).startsWith("/calendar?"));
  });
});

describe("visibleDays and whenParts", () => {
  test("a week is its seven days from Monday, a day is itself", () => {
    assert.deepEqual(visibleDays("week", "2026-10-04"), ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"]);
    assert.deepEqual(visibleDays("day", "2026-10-04"), ["2026-10-04"]);
  });
  test("whenParts reads the business zone: today, the times, and a later end day", () => {
    const now = new Date("2026-09-30T12:00:00Z");
    assert.deepEqual(whenParts("2026-09-30T13:30:00Z", "2026-09-30T14:15:00Z", now, AMS), { today: true, startTime: "15:30", endTime: "16:15", endsLater: false });
    // 23:30 - 00:30 Amsterdam: still the 30th at the start, ends the next local day
    assert.deepEqual(whenParts("2026-09-30T21:30:00Z", "2026-09-30T22:30:00Z", now, AMS), { today: true, startTime: "23:30", endTime: "00:30", endsLater: true });
    assert.equal(whenParts("2026-10-01T08:00:00Z", "2026-10-01T09:00:00Z", now, AMS).today, false);
  });
});

describe("spanTimes", () => {
  test("a 5-minute block reads its real end, not the padded slice end", () => {
    const item = { start: at("2026-09-30T10:30:00Z"), end: at("2026-09-30T10:35:00Z") }; // 12:30 - 12:35 Amsterdam
    assert.deepEqual(spanTimes(item, AMS), { start: "12:30", end: "12:35" });
    assert.equal(daySlices(item, ["2026-09-30"], AMS)[0].end, 12 * 60 + 30 + 23);
  });
  test("it follows the business zone, a +14 zone included", () => {
    const item = { start: at("2026-09-30T10:00:00Z"), end: at("2026-09-30T10:05:00Z") };
    assert.deepEqual(spanTimes(item, "Pacific/Kiritimati"), { start: "00:00", end: "00:05" });
  });
});

describe("isWaiting", () => {
  const history = (...events) => events.map((event) => ({ event }));
  test("the created line of a live pending waits, even after a consent confirmation", () => {
    const d = { status: "pending", expired: false, history: history("created", "consent_confirmed") };
    assert.equal(isWaiting(d, 0), true);
    assert.equal(isWaiting(d, 1), false);
  });
  test("a lapsed or answered booking waits for nobody", () => {
    assert.equal(isWaiting({ status: "pending", expired: true, history: history("created") }, 0), false);
    assert.equal(isWaiting({ status: "confirmed", expired: false, history: history("created", "confirmed") }, 0), false);
  });
});

describe("closedDetail", () => {
  const open = { view: "day", date: "2026-09-30", member: "all", booking: "b1" };
  test("closing the booking in the same window is a close", () => {
    assert.equal(closedDetail(open, { ...open, booking: null }), true);
  });
  test("a new date, view or member dropping the booking is not", () => {
    assert.equal(closedDetail(open, { ...open, booking: null, date: "2026-10-01" }), false);
    assert.equal(closedDetail(open, { ...open, booking: null, view: "week" }), false);
    assert.equal(closedDetail(open, { ...open, booking: null, member: "m2" }), false);
  });
  test("nothing was open, or one still is: no close", () => {
    assert.equal(closedDetail(null, { ...open, booking: null }), false);
    assert.equal(closedDetail({ ...open, booking: null }, { ...open, booking: null }), false);
    assert.equal(closedDetail(open, { ...open, booking: "b2" }), false);
  });
});

describe("telHref", () => {
  test("keeps only + and digits", () => {
    assert.equal(telHref("+31 (0)6 1234-5678"), "tel:+310612345678");
  });
  test("no digits, no link", () => {
    assert.equal(telHref("ask at the desk"), null);
    assert.equal(telHref("+"), null);
  });
});

describe("historyLabel in a +14 zone", () => {
  test("the moved times and days are the business zone's, and the instants ride along", () => {
    const from = "2026-09-30T09:00:00Z"; // 23:00 on the 30th in Kiritimati
    const to = "2026-09-30T11:00:00Z"; // 01:00 on Oct 1st
    const l = historyLabel([{ event: "rescheduled", at: from, actor_name: null, details: { from, to } }], 0, "booking_page", 2, "Pacific/Kiritimati");
    assert.deepEqual([l.params.fromTime, l.params.fromDate, l.params.toDate, l.params.from, l.params.sameDay], ["23:00", "2026-09-30", "2026-10-01", from, false]);
  });
});
