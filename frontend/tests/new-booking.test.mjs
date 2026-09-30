// Pure logic behind the merchant's new-booking, move and block-time panels (ZIF-57 PR3).
// No TZ is pinned: run under TZ=America/New_York and TZ=Pacific/Kiritimati as well.
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import {
  blockPrefill,
  bookingBody,
  defaultService,
  groupSlots,
  initialPick,
  isLatest,
  isUnchanged,
  moveBody,
  pickInstant,
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
  const mine = { newClientEmail: false };
  test("each API answer has its own message", () => {
    assert.equal(submitFailure({ status: 409, code: "slot_taken" }, mine), "slotTaken");
    assert.equal(submitFailure({ status: 409, code: "slot_unavailable" }, mine), "slotUnavailable");
    assert.equal(submitFailure({ status: 409, code: "email_taken" }, mine), "emailTaken");
    assert.equal(submitFailure({ status: 409, code: "invalid_transition" }, mine), "changed");
    assert.equal(submitFailure({ status: 403, code: "owner_only" }, mine), "ownerOnly");
    assert.equal(submitFailure({ status: 401 }, mine), "signedOut");
  });
  test("a 422 is the email's when a new client gave one, else the general banner", () => {
    assert.equal(submitFailure({ status: 422 }, { newClientEmail: true }), "emailInvalid");
    assert.equal(submitFailure({ status: 422 }, mine), "problem");
    assert.equal(submitFailure({ status: 0 }, mine), "problem");
    assert.equal(submitFailure({ status: 500 }, mine), "problem");
  });
});
