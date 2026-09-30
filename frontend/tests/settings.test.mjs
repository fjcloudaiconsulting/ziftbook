// The settings form's pure logic: what changed, what to send, and the option lists.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

import {
  breakMinutes,
  changedKeys,
  draftFrom,
  FIELDS,
  mergeSaved,
  noticeOptions,
  noticeUnit,
  parseWhole,
  settingsBody,
  slotExamples,
  SLOT_STEPS,
  zoneOptions,
} from "../lib/settings.ts";

const saved = {
  timezone: "Europe/Lisbon",
  language: "nl",
  auto_confirm: false,
  slot_step_minutes: 15,
  min_notice_minutes: 60,
  booking_horizon_days: 60,
  buffer_pct: 10,
  pending_ttl_hours: 24,
  max_pending_per_email: 3,
  max_reschedules: 2,
};
const ints = FIELDS.filter((f) => f.kind === "int");

describe("F1 only changed keys are sent", () => {
  test("one of N changed", () => {
    const draft = { ...draftFrom(saved), booking_horizon_days: "90" };
    assert.deepEqual(settingsBody(saved, draft), { body: { booking_horizon_days: 90 }, invalid: [] });
  });
});

describe("F2 compare parsed values", () => {
  test("060 vs 60 is not dirty", () => {
    assert.deepEqual(changedKeys(saved, { ...draftFrom(saved), booking_horizon_days: "060" }), []);
  });
});

describe("F3 parseWhole", () => {
  test("rejects non-whole and out-of-range text for every int field", () => {
    assert.ok(ints.length >= 5);
    for (const { min, max } of ints) {
      for (const text of ["", " ", "2.5", "1e1", "-1", "0x2", String(min - 1), String(max + 1)]) {
        assert.equal(parseWhole(text, min, max), null, `${JSON.stringify(text)} in ${min}-${max}`);
      }
      assert.equal(parseWhole(` ${min} `, min, max), min);
      assert.equal(parseWhole(String(max), min, max), max);
    }
  });
});

describe("F4 invalid text", () => {
  test("is dirty and invalid, never in the body", () => {
    const draft = { ...draftFrom(saved), booking_horizon_days: "abc", buffer_pct: "20" };
    assert.deepEqual(changedKeys(saved, draft), ["booking_horizon_days", "buffer_pct"]);
    assert.deepEqual(settingsBody(saved, draft), { body: { buffer_pct: 20 }, invalid: ["booking_horizon_days"] });
  });
});

describe("F5 noticeOptions", () => {
  test("adds an off-list value once, sorted", () => {
    const list = noticeOptions(90);
    assert.equal(list.filter((n) => n === 90).length, 1);
    assert.deepEqual(list, [...list].sort((a, b) => a - b));
    assert.deepEqual(noticeOptions(60), [0, 30, 60, 120, 240, 720, 1440, 2880, 10080]);
  });
});

describe("F6 noticeUnit", () => {
  test("picks the largest clean unit", () => {
    assert.deepEqual(noticeUnit(1440), { n: 24, unit: "hour" });
    assert.deepEqual(noticeUnit(2880), { n: 2, unit: "day" });
    assert.deepEqual(noticeUnit(10080), { n: 1, unit: "week" });
    assert.deepEqual(noticeUnit(90), { n: 90, unit: "minute" });
    assert.deepEqual(noticeUnit(60), { n: 1, unit: "hour" });
    assert.deepEqual(noticeUnit(0), { n: 0, unit: "none" });
  });
});

describe("F7 zoneOptions", () => {
  test("keeps a saved zone the browser list lacks, once", () => {
    const list = ["Europe/Lisbon", "Europe/Amsterdam"];
    assert.equal(zoneOptions("US/Pacific", list).filter((z) => z === "US/Pacific").length, 1);
    assert.deepEqual(zoneOptions("Europe/Lisbon", list), list);
  });
});

describe("F8 published", () => {
  test("never in the body", () => {
    const draft = { ...draftFrom(saved), published: true, buffer_pct: "20" };
    const { body } = settingsBody({ ...saved, published: false }, draft);
    assert.deepEqual(body, { buffer_pct: 20 });
    assert.ok(!FIELDS.some((f) => f.key === "published"));
  });
});

describe("F9 bounds match the backend", () => {
  const props = JSON.parse(readFileSync(new URL("../../backend/openapi.json", import.meta.url), "utf8")).components.schemas["BusinessSettings-Input"].properties;
  test("int bounds", () => {
    for (const f of FIELDS.filter((f) => f.min !== undefined)) {
      assert.equal(f.min, props[f.key].minimum, `${f.key} min`);
      assert.equal(f.max, props[f.key].maximum, `${f.key} max`);
    }
  });
  test("slot steps and keys", () => {
    assert.deepEqual(SLOT_STEPS, props.slot_step_minutes.enum);
    for (const f of FIELDS) assert.ok(f.key in props, f.key);
  });
});

describe("F10 numeric selects", () => {
  test("a picked 30 is the number 30", () => {
    const { body } = settingsBody(saved, { ...draftFrom(saved), slot_step_minutes: "30" });
    assert.equal(body.slot_step_minutes, 30);
    assert.equal(typeof body.slot_step_minutes, "number");
  });
  test("15 vs saved 15 is not dirty", () => {
    assert.deepEqual(changedKeys(saved, { ...draftFrom(saved), slot_step_minutes: "15" }), []);
  });
  test("booleans compare as booleans", () => {
    assert.deepEqual(changedKeys(saved, { ...draftFrom(saved), auto_confirm: true }), ["auto_confirm"]);
    assert.deepEqual(settingsBody(saved, { ...draftFrom(saved), auto_confirm: true }).body, { auto_confirm: true });
  });
});

describe("F11 mergeSaved", () => {
  test("an unsent edit survives, a sent key takes the server value", () => {
    const draft = { ...draftFrom(saved), buffer_pct: "20", max_reschedules: "5" };
    const data = { ...saved, buffer_pct: 20, max_reschedules: 2, published: true };
    const merged = mergeSaved(draft, data, ["buffer_pct"]);
    assert.equal(merged.max_reschedules, "5");
    assert.equal(merged.buffer_pct, "20");
    assert.ok(!("published" in merged));
  });
});

describe("guards", () => {
  test("G1 changedKeys follow FIELDS order", () => {
    const draft = { ...draftFrom(saved), max_reschedules: "3", timezone: "Europe/Paris", buffer_pct: "5" };
    assert.deepEqual(changedKeys(saved, draft), ["timezone", "buffer_pct", "max_reschedules"]);
  });
  test("G2 untouched draft is clean", () => {
    assert.deepEqual(changedKeys(saved, draftFrom(saved)), []);
  });
  test("hint helpers", () => {
    assert.deepEqual(slotExamples(15), [540, 555, 570]);
    assert.equal(breakMinutes(10), 6);
    assert.equal(breakMinutes(15), 9);
    assert.equal(breakMinutes(1), 1);
  });
});
