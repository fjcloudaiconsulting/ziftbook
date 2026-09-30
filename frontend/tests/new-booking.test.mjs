// Pure logic behind the merchant's new-booking, move and block-time panels (ZIF-57 PR3).
// No TZ is pinned: run under TZ=America/New_York and TZ=Pacific/Kiritimati as well.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

import {
  availabilityDays,
  blockPrefill,
  bookingBody,
  defaultService,
  groupSlots,
  initialPick,
  hhmm,
  initialStrip,
  isLatest,
  memberForService,
  messageShown,
  isUnchanged,
  moveBody,
  pickInstant,
  searchStep,
  searchTerm,
  shiftStrip,
  slotAt,
  stripStart,
  submitFailure,
} from "../lib/new-booking.ts";

const AMS = "Europe/Amsterdam";

describe("slotAt (F21)", () => {
  test("rounds down to the step: 09:37 and 09:44 both open the 09:30 slot", () => {
    assert.equal(slotAt(37, 9, 15, 18), "09:30");
    assert.equal(slotAt(44, 9, 15, 18), "09:30");
    assert.equal(slotAt(45, 9, 15, 18), "09:45");
  });
  test("honours the step: 20 minutes", () => {
    assert.equal(slotAt(59, 9, 20, 18), "09:40");
  });
  test("clamps inside the shown hours: never before the first hour, never a slot that starts at the end", () => {
    assert.equal(slotAt(-10, 9, 15, 18), "09:00");
    assert.equal(slotAt(9 * 60, 9, 15, 18), "17:45");
    assert.equal(slotAt(10_000, 9, 15, 18), "17:45");
  });
});

describe("bookingBody (F23)", () => {
  const base = { serviceId: "s1", startsAt: "2026-09-30T13:30:00.000Z", memberId: "m1", override: false };
  test("an existing client sends client_id and never new_client", () => {
    const body = bookingBody({ ...base, clientId: "c1", newClient: { name: "ignored", phone: "", email: "" } });
    assert.equal(body.client_id, "c1");
    assert.ok(!("new_client" in body));
  });
  test("a new client sends new_client and never client_id; blank or spaces-only phone and email are left out", () => {
    const body = bookingBody({ ...base, clientId: null, newClient: { name: "  Marta Silva ", phone: "   ", email: "" } });
    assert.ok(!("client_id" in body));
    assert.deepEqual(body.new_client, { name: "Marta Silva" });
    const full = bookingBody({ ...base, clientId: null, newClient: { name: "M", phone: " +351 913 000 111 ", email: " m@x.pt " } });
    assert.deepEqual(full.new_client, { name: "M", phone: "+351 913 000 111", email: "m@x.pt" });
  });
  test("anyone sends no member_id", () => {
    assert.ok(!("member_id" in bookingBody({ ...base, memberId: null, clientId: "c1", newClient: null })));
    assert.ok(!("member_id" in bookingBody({ ...base, memberId: "", clientId: "c1", newClient: null })));
    assert.equal(bookingBody({ ...base, clientId: "c1", newClient: null }).member_id, "m1");
  });
  test("override is sent only with a person, and only when true", () => {
    assert.equal(bookingBody({ ...base, override: true, clientId: "c1", newClient: null }).override, true);
    assert.ok(!("override" in bookingBody({ ...base, override: true, memberId: null, clientId: "c1", newClient: null })));
    assert.ok(!("override" in bookingBody({ ...base, override: false, clientId: "c1", newClient: null })));
  });
});

describe("moveBody", () => {
  test("names the person, sends override only when true", () => {
    assert.deepEqual(moveBody({ startsAt: "2026-09-30T13:30:00.000Z", memberId: "m1", override: false }), { starts_at: "2026-09-30T13:30:00.000Z", member_id: "m1" });
    assert.equal(moveBody({ startsAt: "x", memberId: "m1", override: true }).override, true);
  });
});

describe("initialPick (F25)", () => {
  const slots = ["2026-09-30T13:30:00.000Z", "2026-09-30T13:45:00.000Z"];
  test("an at among the offered starts is selected, whatever the instant's spelling", () => {
    assert.deepEqual(initialPick("2026-09-30T15:45:00+02:00", slots, AMS), { kind: "slot", slot: slots[1] });
  });
  test("an at that is not offered opens Pick another time, prefilled with its local day and time", () => {
    assert.deepEqual(initialPick("2026-09-30T14:10:00.000Z", slots, AMS), { kind: "other", date: "2026-09-30", time: "16:10" });
    assert.deepEqual(initialPick("2026-09-30T22:30:00.000Z", slots, AMS), { kind: "other", date: "2026-10-01", time: "00:30" });
  });
  test("no at, nothing picked", () => {
    assert.deepEqual(initialPick(null, slots, AMS), { kind: "none" });
  });
});

describe("pickInstant", () => {
  test("a slot is itself; another time is its local date and time as an instant", () => {
    assert.equal(pickInstant({ kind: "slot", slot: "2026-09-30T13:30:00.000Z" }, AMS), "2026-09-30T13:30:00.000Z");
    assert.equal(pickInstant({ kind: "other", date: "2026-09-30", time: "16:10" }, AMS), "2026-09-30T14:10:00.000Z");
  });
  test("an unfinished another time, or no pick, is null and never throws", () => {
    assert.equal(pickInstant({ kind: "other", date: "", time: "16:10" }, AMS), null);
    assert.equal(pickInstant({ kind: "other", date: "2026-09-30", time: "" }, AMS), null);
    assert.equal(pickInstant({ kind: "none" }, AMS), null);
  });
});

describe("the day strip", () => {
  test("starts at the anchor, never before today", () => {
    assert.equal(stripStart("2026-10-05", "2026-09-30"), "2026-10-05");
    assert.equal(stripStart("2026-09-01", "2026-09-30"), "2026-09-30");
  });
  test("next and previous move a week; previous stops at today", () => {
    assert.equal(shiftStrip("2026-09-30", 1, "2026-09-30"), "2026-10-07");
    assert.equal(shiftStrip("2026-10-07", -1, "2026-09-30"), "2026-09-30");
    assert.equal(shiftStrip("2026-10-02", -1, "2026-09-30"), "2026-09-30");
  });
  test("slots group by their local day, in order", () => {
    const grouped = groupSlots(["2026-09-30T22:30:00.000Z", "2026-09-30T09:00:00.000Z", "2026-10-01T08:00:00.000Z"], AMS);
    assert.deepEqual(Object.keys(grouped), ["2026-09-30", "2026-10-01"]);
    assert.deepEqual(grouped["2026-10-01"], ["2026-09-30T22:30:00.000Z", "2026-10-01T08:00:00.000Z"]);
  });
});

describe("isLatest", () => {
  test("only the newest request's answer is used", () => {
    assert.equal(isLatest(3, 3), true);
    assert.equal(isLatest(2, 3), false);
  });
});

describe("searchTerm", () => {
  test("a blank or spaces-only query is no search", () => {
    assert.equal(searchTerm(""), null);
    assert.equal(searchTerm("   "), null);
    assert.equal(searchTerm("  Mar "), "Mar");
  });
});

describe("blockPrefill", () => {
  test("starts at the slot in the business zone, an hour long", () => {
    assert.deepEqual(blockPrefill("2026-09-30T13:30:00.000Z", AMS), { firstDay: "2026-09-30", startTime: "15:30", endTime: "16:30" });
  });
  test("never wraps past midnight", () => {
    assert.equal(blockPrefill("2026-09-30T21:30:00.000Z", AMS).endTime, "23:59");
    assert.equal(blockPrefill("2026-09-30T20:30:00.000Z", AMS).endTime, "23:30");
  });
});

describe("defaultService", () => {
  const services = [
    { id: "a", archived: false, worker_ids: ["m1"] },
    { id: "b", archived: false, worker_ids: ["m2"] },
    { id: "c", archived: true, worker_ids: ["m2"] },
  ];
  test("the first service the prefilled person does, never an archived one", () => {
    assert.equal(defaultService(services, "m2").id, "b");
  });
  test("none for a person who does nothing; the first live one when nobody is prefilled", () => {
    assert.equal(defaultService(services, "m9"), null);
    assert.equal(defaultService(services, null).id, "a");
    assert.equal(defaultService([{ id: "c", archived: true, worker_ids: [] }], null), null);
  });
});

describe("isUnchanged", () => {
  const booking = { starts_at: "2026-09-30T13:30:00.000Z", worker_id: "m1" };
  test("the same start (whatever its spelling) and person is no move", () => {
    assert.equal(isUnchanged("2026-09-30T15:30:00+02:00", "m1", booking), true);
  });
  test("a different time or person is a move", () => {
    assert.equal(isUnchanged("2026-09-30T13:45:00.000Z", "m1", booking), false);
    assert.equal(isUnchanged("2026-09-30T13:30:00.000Z", "m2", booking), false);
    assert.equal(isUnchanged(null, "m1", booking), false);
  });
});

describe("submitFailure", () => {
  test("without an override any slot 409 is just taken", () => {
    assert.equal(submitFailure({ status: 409, code: "slot_taken" }, false, "Ana"), "slotTaken");
    assert.equal(submitFailure({ status: 409, code: "slot_unavailable" }, false, "Ana"), "slotTaken");
    assert.equal(submitFailure({ status: 409 }, false, null), "slotTaken");
  });
  test("with an override slot_unavailable names the person, only when one is chosen", () => {
    assert.equal(submitFailure({ status: 409, code: "slot_unavailable" }, true, "Ana"), "memberUnavailable");
    assert.equal(submitFailure({ status: 409, code: "slot_unavailable" }, true, null), "slotTaken");
    assert.equal(submitFailure({ status: 409, code: "slot_taken" }, true, "Ana"), "slotTaken");
    assert.equal(submitFailure({ status: 409, code: "other" }, true, "Ana"), "problem");
  });
  test("the other answers keep their own messages", () => {
    assert.equal(submitFailure({ status: 409, code: "email_taken" }, false, null), "emailTaken");
    assert.equal(submitFailure({ status: 409, code: "invalid_transition" }, true, null), "changed");
    assert.equal(submitFailure({ status: 401 }, false, null), "signedOut");
  });
  test("403 is owner-only only when the code says so", () => {
    assert.equal(submitFailure({ status: 403, code: "owner_only" }, false, null), "ownerOnly");
    assert.equal(submitFailure({ status: 403 }, false, null), "problem");
    assert.equal(submitFailure({ status: 403, code: "name_required" }, false, null), "problem");
  });
  test("a 422 (which never names a field), a network failure or a 500 is the general banner", () => {
    assert.equal(submitFailure({ status: 422, code: "invalid_request" }, false, null), "problem");
    assert.equal(submitFailure({ status: 0 }, false, null), "problem");
    assert.equal(submitFailure({ status: 500 }, false, null), "problem");
  });
});

describe("searchStep", () => {
  test("an empty or blank query never calls", () => {
    assert.equal(searchStep("", 1, 1).call, false);
    assert.equal(searchStep("  ", 1, 1).call, false);
    assert.deepEqual(searchStep(" Mar ", 1, 1), { term: "Mar", call: true, apply: true });
  });
  test("only the latest request's answer applies", () => {
    assert.equal(searchStep("Mar", 2, 3).apply, false);
    assert.equal(searchStep("Mar", 3, 3).apply, true);
  });
});

describe("memberForService", () => {
  test("the person stays when they do the service, or nobody is chosen", () => {
    assert.equal(memberForService("m1", ["m1", "m2"], ""), "m1");
    assert.equal(memberForService("", ["m2"], "x"), "");
  });
  test("otherwise the fallback", () => {
    assert.equal(memberForService("m1", ["m2"], ""), "");
    assert.equal(memberForService("m1", [], "m2"), "m2");
  });
});

describe("initialStrip", () => {
  test("a future start anchors and selects its own day", () => {
    assert.deepEqual(initialStrip("2026-10-05", "2026-10-01", "2026-09-30"), { anchor: "2026-10-05", day: "2026-10-05" });
  });
  test("a past start lands on today, selected", () => {
    assert.deepEqual(initialStrip("2026-09-01", "2026-09-01", "2026-09-30"), { anchor: "2026-09-30", day: "2026-09-30" });
  });
  test("no start: the calendar's date (never past), nothing selected", () => {
    assert.deepEqual(initialStrip(null, "2026-10-02", "2026-09-30"), { anchor: "2026-10-02", day: null });
    assert.deepEqual(initialStrip(null, "2026-09-01", "2026-09-30"), { anchor: "2026-09-30", day: null });
  });
});

describe("messageShown", () => {
  const msg = { booking: "b1", date: "2026-09-30", view: "day" };
  const now = { booking: "b1", date: "2026-09-30", view: "day", panel: null };
  test("shown only where it was made", () => {
    assert.equal(messageShown(msg, now), true);
    assert.equal(messageShown(msg, { ...now, date: "2026-10-01" }), false);
    assert.equal(messageShown(msg, { ...now, view: "week" }), false);
    assert.equal(messageShown(msg, { ...now, panel: "new" }), false);
    assert.equal(messageShown(msg, { ...now, booking: null }), false);
    assert.equal(messageShown(null, now), false);
  });
});

describe("hhmm", () => {
  test("pads, and midnight wraps", () => {
    assert.equal(hhmm(75), "01:15");
    assert.equal(hhmm(1440), "00:00");
    assert.equal(hhmm(0), "00:00");
  });
});

// The merchant availability route takes local DAYS (backend availability.py Day, inclusive, at most 14),
// not instants: the generated client types both as string, so only this pins the contract.
describe("availabilityDays", () => {
  test("seven local days, inclusive, as YYYY-MM-DD", () => {
    assert.deepEqual(availabilityDays("2026-10-01"), { from: "2026-10-01", to: "2026-10-07" });
  });
  test("matches the API's date format for both params", () => {
    const api = JSON.parse(readFileSync(new URL("../../backend/openapi.json", import.meta.url), "utf8"));
    const params = api.paths["/api/services/{service_id}/availability"].get.parameters;
    for (const name of ["from", "to"]) assert.equal(params.find((q) => q.name === name).schema.format, "date");
    for (const value of Object.values(availabilityDays("2026-10-25"))) assert.match(value, /^\d{4}-\d{2}-\d{2}$/);
  });
});
