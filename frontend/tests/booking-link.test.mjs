// Logic behind the guest booking page (ZIF-54 spec §6, §7, test 30): where the page starts, which
// screen every answer leads to, which sentence a copy_key reads as, and a week of free times.
process.env.TZ = "America/Sao_Paulo";

import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { takeToken } from "../lib/account.ts";
import { answerScreen, copyMessage, firstStage, refundMessage, slotsByDay, weekDays } from "../lib/booking-link.ts";

const TENANT = "0192f3a4-5b6c-7d8e-9f01-23456789abcd";
const SECRET = "Ab3_-".repeat(8) + "xyz"; // 43 URL-safe characters

describe("the link leaves the address bar", () => {
  test("guard: takeToken returns the fragment and leaves the clean /{locale}/booking address", () => {
    const later = [];
    const place = {
      location: new URL(`https://app.example/nl/booking#${TENANT}.${SECRET}`),
      history: {
        replaceState(_data, _unused, next) {
          place.location = new URL(next, place.location);
        },
      },
      setTimeout: (callback) => later.push(callback),
    };
    assert.equal(takeToken(place), `${TENANT}.${SECRET}`);
    later.forEach((callback) => callback());
    assert.equal(place.location.href, "https://app.example/nl/booking");
  });
});

describe("where the page starts", () => {
  test("fence: no fragment reads the booking with the cookie, never jumps to 'no longer active'", () => {
    // Wrong implementation killed: treating a reload (no fragment) as a dead link, which would
    // lock the client out of the page they just opened.
    assert.equal(firstStage(null), "read");
  });

  test("a link that looks like one is exchanged; a mangled one is not active, with no request", () => {
    assert.equal(firstStage(`${TENANT}.${SECRET}`), "exchange");
    for (const token of ["", "garbage", SECRET, `${TENANT}.${SECRET}x`, `${TENANT}.${SECRET.slice(1)}=`]) {
      assert.equal(firstStage(token), "notActive", JSON.stringify(token));
    }
  });
});

describe("which screen an answer leads to", () => {
  test("fence: every 404 and 409 code the routes send has its own screen (spec §4, §7)", () => {
    // Wrong implementation killed: one generic branch for 409 (or 404), which would show a
    // retry banner for a changed booking or hide the "another booking" screen.
    const cases = [
      [{ status: 404, code: "link_expired" }, "notActive"],
      [{ status: 404 }, "notActive"],
      [{ status: 409, code: "link_changed" }, "anotherBooking"],
      [{ status: 409, code: "terms_changed" }, "termsChanged"],
      [{ status: 409, code: "changed" }, "changed"],
      [{ status: 409, code: "slot_taken" }, "slotTaken"],
      [{ status: 409, code: "slot_unavailable" }, "slotTaken"],
      [{ status: 409, code: "not_allowed" }, "notAllowed"],
      [{ status: 409, code: "nothing_to_confirm" }, "refresh"],
    ];
    for (const [answer, screen] of cases) assert.equal(answerScreen(answer), screen, JSON.stringify(answer));
  });

  test("a network error, 429, 503 or anything unknown keeps the screen and offers a retry", () => {
    for (const answer of [{ status: 0 }, { status: 429 }, { status: 503, code: "busy" }, { status: 422 }, { status: 500 }, { status: 409 }, { status: 409, code: "new" }]) {
      assert.equal(answerScreen(answer), "retry", JSON.stringify(answer));
    }
  });

  test("a success is the booking", () => {
    assert.equal(answerScreen({ status: 200 }), "booking");
    assert.equal(answerScreen({ status: 204 }), "booking");
  });
});

describe("the engine's sentence", () => {
  test("each copy_key has its own sentence, and pending says nothing is refunded", () => {
    assert.equal(copyMessage("full_refund_reschedule"), "full_refund_reschedule");
    assert.equal(copyMessage("full_refund_no_reschedule"), "full_refund_no_reschedule");
    assert.equal(copyMessage("no_refund_reschedule"), "no_refund_reschedule");
    assert.equal(copyMessage("no_refund_no_reschedule"), "no_refund_no_reschedule");
    assert.equal(copyMessage("started"), "started");
    assert.equal(copyMessage("pending"), "pending");
  });

  test("fence: an unknown key shows no sentence, never a free-cancellation promise", () => {
    // Wrong implementation killed: a fallback to a default sentence.
    assert.equal(copyMessage("partial_refund"), null);
    assert.equal(copyMessage(""), null);
  });

  test("fence: the refund sentence restated on cancel follows the refund, and a pending booking has nothing to refund", () => {
    // Wrong implementation killed: reading refund_pct alone, which calls an unpaid booking "not refunded".
    assert.equal(refundMessage({ refund_pct: 100, copy_key: "full_refund_reschedule" }), "refundFull");
    assert.equal(refundMessage({ refund_pct: 0, copy_key: "no_refund_reschedule" }), "refundNone");
    assert.equal(refundMessage({ refund_pct: 0, copy_key: "pending" }), "pending");
  });
});

describe("a week of free times", () => {
  test("seven days from the first", () => {
    assert.deepEqual(weekDays("2026-10-29"), ["2026-10-29", "2026-10-30", "2026-10-31", "2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04"]);
  });

  test("fence: slots group by the business's local day, not the UTC date or the browser's", () => {
    // Wrong implementation killed: slot.slice(0, 10) (UTC) or the browser zone (TZ above is Sao Paulo).
    const slots = ["2026-10-29T23:30:00Z", "2026-10-30T08:00:00Z", "2026-10-30T09:00:00Z"];
    const days = slotsByDay(slots, "Europe/Amsterdam");
    assert.deepEqual(days.get("2026-10-30"), ["2026-10-29T23:30:00Z", "2026-10-30T08:00:00Z", "2026-10-30T09:00:00Z"]);
    assert.equal(days.get("2026-10-29"), undefined);
  });
});
