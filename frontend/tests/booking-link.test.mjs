// Logic behind the guest booking page (ZIF-54 spec §6, §7, test 30): where the page starts, which
// screen every answer leads to, which sentence a copy_key reads as, and a week of free times.
process.env.TZ = "America/Sao_Paulo";

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

import { takeToken } from "../lib/account.ts";
import { answerScreen, copyMessage, firstStage, localWhen, pickerDay, refundMessage, slotsByDay, weekDays, withTaken } from "../lib/booking-link.ts";

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

  test("fence: a 409 code named like an Object.prototype member is unknown too (kills `in` for Object.hasOwn)", () => {
    for (const code of ["toString", "constructor", "__proto__", "hasOwnProperty"]) {
      assert.equal(answerScreen({ status: 409, code }), "retry", code);
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

  test("fence: a just-taken time stays listed once after the week reloads (ZIF-149)", () => {
    // Wrong implementations killed: the reloaded slots unchanged (the taken time vanishes instead of
    // showing struck through) and a plain concat (listed twice when the server still offers it).
    const reloaded = ["2026-10-20T07:00:00Z", "2026-10-20T09:00:00Z"];
    assert.deepEqual(withTaken(reloaded, "2026-10-20T08:00:00Z"), ["2026-10-20T07:00:00Z", "2026-10-20T08:00:00Z", "2026-10-20T09:00:00Z"]);
    assert.deepEqual(withTaken(reloaded, "2026-10-20T09:00:00Z"), reloaded);
    assert.deepEqual(withTaken(reloaded, undefined), reloaded);
  });
});

describe("one picker for booking and rescheduling (ZIF-149)", () => {
  test("fence: the picker's day is the chosen one only inside this week's strip, else the first day with times", () => {
    // Wrong implementations killed: keeping a chosen day outside the strip that still has times (a 409's
    // taken day after Next week: nothing pressed, only the struck time shown) and keeping a day with none.
    const days = weekDays("2026-10-22");
    const byDay = new Map([["2026-10-20", ["2026-10-20T08:30:00Z"]], ["2026-10-23", ["2026-10-23T08:00:00Z"]], ["2026-10-24", []]]);
    assert.equal(pickerDay(days, byDay, "2026-10-20"), "2026-10-23");
    assert.equal(pickerDay(days, byDay, "2026-10-24"), "2026-10-23");
    assert.equal(pickerDay(days, byDay, null), "2026-10-23");
    assert.equal(pickerDay(days, new Map([...byDay, ["2026-10-25", ["2026-10-25T09:00:00Z"]]]), "2026-10-25"), "2026-10-25");
    assert.equal(pickerDay(days, new Map(), null), null);
    assert.match(read("../app/[locale]/_ui/picker.tsx"), /const day = pickerDay\(days, byDay, chosenDay\);/);
  });

  const read = (rel) => readFileSync(new URL(rel, import.meta.url), "utf8");

  test("fence: both pages use the shared picker and neither draws its own day strip or times", () => {
    // Wrong implementation killed: the reschedule page (or the booking page) keeping its own inline picker.
    for (const page of ["../app/[locale]/booking/manage-booking.tsx", "../app/[locale]/[slug]/checkout.tsx"]) {
      const src = read(page);
      assert.match(src, /import \{[^}]*\bPicker\b[^}]*\} from "\.\.\/_ui\/picker"/, page);
      assert.doesNotMatch(src, /styles\.(days|slots|parts)\b/, page);
    }
    // The controller only routes the steps; it must not grow its own strip either.
    assert.doesNotMatch(read("../app/[locale]/[slug]/booking-page.tsx"), /styles\.(days|slots|parts)\b/);
  });

  test("fence: the marks are drawn only inside the picker, never on the console's day strips", () => {
    // Wrong implementations killed: an unscoped `.days .button::after` (dots under the working-hours
    // days in the console, which share .days) and marks that were never written.
    const css = read("../app/[locale]/_ui/ui.module.css");
    for (const line of css.split("\n").filter((l) => /::after|line-through/.test(l) && /\.(days|slots)\b/.test(l))) {
      assert.match(line, /^\.picker \.(days|slots) /, line);
    }
    assert.match(css, /^\.picker \.days \.button::after \{/m);
    assert.match(css, /^\.picker \.days \.button:disabled span:last-child \{[^}]*line-through/m);
    assert.match(css, /^\.picker \.slots \.button:disabled \{[^}]*line-through/m);
  });
});

describe("the free-cancellation deadline", () => {
  test("fence: free_until reads in the business's time zone, not UTC or the browser's", () => {
    // Wrong implementation killed: formatting without timeZone (the browser's Sao Paulo, above) or in UTC.
    // 08:00Z on 16 October is 10:00 in Amsterdam (CEST), 05:00 in Sao Paulo.
    assert.deepEqual(localWhen("2026-10-16T08:00:00Z", "Europe/Amsterdam", "en-GB"), { date: "Friday 16 October", time: "10:00" });
    assert.deepEqual(localWhen("2026-10-15T23:30:00Z", "Europe/Amsterdam", "en-GB"), { date: "Friday 16 October", time: "01:30" });
  });

  test("every language's sentence carries the date and the time", () => {
    for (const locale of ["en", "nl", "pt"]) {
      const sentence = JSON.parse(readFileSync(new URL(`../messages/${locale}.json`, import.meta.url), "utf8")).BookingLink.freeUntil;
      assert.match(sentence, /\{date\}, \{time\}/, locale);
    }
  });
});
