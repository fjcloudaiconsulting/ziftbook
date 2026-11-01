// The settings form's pure logic: what changed, what to send, and the option lists.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

import {
  asciiEmail,
  breakMinutes,
  changedKeys,
  draftFrom,
  effective,
  emailProblem,
  FIELDS,
  mergeSaved,
  noticeOptions,
  noticeUnit,
  parseWhole,
  paymentsAction,
  refusedKeys,
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
  free_cancellation_hours: 48,
  reschedule_cutoff_hours: 24,
  max_reschedules: 2,
  cancellation_policy_text: "Please cancel at least 48 hours ahead.",
  workers_edit_own_hours: false,
  contact_email: null,
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
    const draft = { ...draftFrom(saved), buffer_pct: "020", max_reschedules: "5" };
    const data = { ...saved, buffer_pct: 20, max_reschedules: 2, published: true };
    const merged = mergeSaved(draft, data, ["buffer_pct"]);
    assert.equal(merged.max_reschedules, "5");
    assert.equal(merged.buffer_pct, "20"); // the server's value, not the typed "020"
    assert.ok(!("published" in merged));
  });  test("an empty policy and an unticked box from the server are kept", () => {
    const data = { ...saved, cancellation_policy_text: "", workers_edit_own_hours: false };
    const merged = mergeSaved({ ...draftFrom(saved), cancellation_policy_text: "x", workers_edit_own_hours: true }, data, ["cancellation_policy_text", "workers_edit_own_hours"]);
    assert.deepEqual([merged.cancellation_policy_text, merged.workers_edit_own_hours], ["", false]);
    assert.deepEqual(changedKeys(data, merged), []);
  });
});

describe("F12 policy text is sent exactly as typed", () => {
  test("a trailing space is a change and is kept", () => {
    const text = `${saved.cancellation_policy_text} `;
    const draft = { ...draftFrom(saved), cancellation_policy_text: text };
    assert.deepEqual(settingsBody(saved, draft), { body: { cancellation_policy_text: text }, invalid: [] });
  });
  test("clearing it sends the empty string", () => {
    const draft = { ...draftFrom(saved), cancellation_policy_text: "" };
    assert.deepEqual(settingsBody(saved, draft), { body: { cancellation_policy_text: "" }, invalid: [] });
  });
});

describe("F13 cancelling and team keys", () => {
  test("all four are sent, typed, in page order", () => {
    const draft = {
      ...draftFrom(saved),
      workers_edit_own_hours: true,
      cancellation_policy_text: "No refunds.",
      reschedule_cutoff_hours: "0",
      free_cancellation_hours: "720",
    };
    const { body, invalid } = settingsBody(saved, draft);
    assert.deepEqual(invalid, []);
    assert.deepEqual(Object.entries(body), [
      ["free_cancellation_hours", 720],
      ["reschedule_cutoff_hours", 0],
      ["cancellation_policy_text", "No refunds."],
      ["workers_edit_own_hours", true],
    ]);
  });
  test("721 hours is invalid, never sent", () => {
    const { body, invalid } = settingsBody(saved, { ...draftFrom(saved), free_cancellation_hours: "721" });
    assert.deepEqual([body, invalid], [{}, ["free_cancellation_hours"]]);
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
  test("G3 effective follows a valid draft, else the saved value", () => {
    assert.equal(effective(saved, { ...draftFrom(saved), buffer_pct: "20" }, "buffer_pct"), 20);
    assert.equal(effective(saved, { ...draftFrom(saved), buffer_pct: "x" }, "buffer_pct"), 10);
    assert.equal(effective(saved, { ...draftFrom(saved), slot_step_minutes: "30" }, "slot_step_minutes"), 30);
    assert.equal(effective(saved, { ...draftFrom(saved), max_reschedules: "1e3" }, "max_reschedules"), 2);
    assert.equal(effective(saved, { ...draftFrom(saved), max_reschedules: "0" }, "max_reschedules"), 0);
  });
  test("hint helpers", () => {
    assert.deepEqual(slotExamples(15), [540, 555, 570]);
    assert.equal(breakMinutes(10), 6);
    assert.equal(breakMinutes(15), 9);
    assert.equal(breakMinutes(1), 1);
    assert.equal(breakMinutes(2), 2);
  });
});

describe("F1 the payments panel offers only actions the API can serve", () => {
  test("none and onboarding connect, ready opens the dashboard, off or an unsupported country offers nothing", () => {
    assert.equal(paymentsAction("none", true), "connect");
    assert.equal(paymentsAction("onboarding", true), "continue");
    assert.equal(paymentsAction("ready", true), "dashboard");
    assert.equal(paymentsAction("off", true), null);
    for (const state of ["none", "onboarding", "ready", "off"]) assert.equal(paymentsAction(state, false), null);
  });
});

describe("F2 the payment switch", () => {
  test("guard: toggling payment_check is a change and is posted", () => {
    const withCheck = { ...saved, payment_check: false };
    assert.deepEqual(changedKeys(withCheck, { ...draftFrom(withCheck), payment_check: true }), ["payment_check"]);
    assert.deepEqual(settingsBody(withCheck, { ...draftFrom(withCheck), payment_check: true }).body, { payment_check: true });
  });
});

describe("ZIF-99 the contact email for clients", () => {
  const withEmail = { ...saved, contact_email: "hello@salonbloom.nl" };

  test("fence: it sits in Your business, right after the email language", () => {
    // Wrong implementation killed: the key missing from FIELDS (never sent, never dirty) or listed out of page order.
    const keys = FIELDS.map((f) => f.key);
    assert.equal(keys[keys.indexOf("language") + 1], "contact_email");
  });

  test("fence: no address is an empty box, and an untouched empty box is clean", () => {
    // Wrong implementation killed: String(null) putting the text "null" in the box (and then sending it).
    assert.equal(draftFrom(saved).contact_email, "");
    assert.deepEqual(changedKeys(saved, draftFrom(saved)), []);
    assert.equal(draftFrom(withEmail).contact_email, "hello@salonbloom.nl");
  });

  test("fence: an address is sent the way the API stores it; spaces and capitals alone are no change", () => {
    // Wrong implementation killed: an unsaved-changes bar that never goes away after the server lowercases the address.
    assert.deepEqual(settingsBody(saved, { ...draftFrom(saved), contact_email: " Hello@SalonBloom.nl " }), {
      body: { contact_email: "hello@salonbloom.nl" },
      invalid: [],
    });
    assert.deepEqual(changedKeys(withEmail, { ...draftFrom(withEmail), contact_email: " HELLO@salonbloom.nl" }), []);
  });

  test("fence: emptying the box removes the address (null), never an empty string", () => {
    // Wrong implementation killed: sending "", which the API refuses as an address.
    for (const text of ["", "   "]) {
      assert.deepEqual(settingsBody(withEmail, { ...draftFrom(withEmail), contact_email: text }), { body: { contact_email: null }, invalid: [] });
    }
  });

  test("fence: an address with accents is refused with its own message; a malformed one with the usual one", () => {
    // Wrong implementation killed: sending an address the API refuses (non-ASCII cannot go in a mail header),
    // or one generic message that never says why an accented address is refused.
    assert.equal(emailProblem("josé@salon.nl"), "accents");
    assert.equal(emailProblem("josé@café.nl"), "accents");
    for (const text of ["hello", "hello@salon", "@salon.nl", "a@b@c.nl", "a b@c.nl", "a@b.nl, c@d.nl", "<a@b.nl>", "a@b.nl\nBcc: c@d.nl", "=?utf-8?q?x?=@b.nl", `${"a".repeat(250)}@b.nl`]) {
      assert.equal(emailProblem(text), "invalid", JSON.stringify(text));
    }
    for (const text of ["", "  ", "hello@salonbloom.nl", " Hello@SalonBloom.NL ", "first.last+shop@xn--caf-dma.nl"]) {
      assert.equal(emailProblem(text), null, JSON.stringify(text));
    }
    const { body, invalid } = settingsBody(saved, { ...draftFrom(saved), contact_email: "josé@salon.nl", buffer_pct: "20" });
    assert.deepEqual([body, invalid], [{ buffer_pct: 20 }, ["contact_email"]]);
  });

  test("every language has the field's words, and Your business says where clients reach you", () => {
    for (const locale of ["en", "nl", "pt"]) {
      const t = JSON.parse(readFileSync(new URL(`../messages/${locale}.json`, import.meta.url), "utf8")).Console.settings;
      for (const key of ["contactEmailLabel", "contactEmailOptional", "contactEmailHint", "contactEmailInvalid"]) assert.ok(t[key], `${locale}.${key}`);
      assert.ok(t.changed.contact_email, `${locale}.changed.contact_email`);
    }
    const desc = (locale) => JSON.parse(readFileSync(new URL(`../messages/${locale}.json`, import.meta.url), "utf8")).Console.settings.yourBusinessDesc;
    assert.equal(desc("en"), "Your time zone, the default language for client emails, and where clients can reach you.");
    assert.equal(desc("nl"), "Je tijdzone, de standaardtaal voor e-mails aan klanten en waar klanten je kunnen bereiken.");
    assert.equal(desc("pt"), "Seu fuso horário, o idioma padrão dos e-mails para clientes e onde os clientes podem falar com você.");
  });
});

describe("ZIF-99 which field a refused save marks", () => {
  test("fence: only the API's contact_email_invalid marks the address; any other refusal marks no field", () => {
    // Wrong implementation killed: every 422 of a save that included the address blamed on it (a refused
    // time zone would then point at the address, and every retry fail the same way).
    assert.deepEqual(refusedKeys(422, "contact_email_invalid"), ["contact_email"]);
    assert.deepEqual(refusedKeys(422, "invalid_request"), []);
    assert.deepEqual(refusedKeys(422, undefined), []);
    assert.deepEqual(refusedKeys(400, "contact_email_invalid"), []);
    assert.deepEqual(refusedKeys(200, undefined), []);
  });

  test("fence: the page marks, focuses and keeps the bar for exactly those keys", () => {
    // Wrong implementation killed: the branch dropped (the address shows only the generic banner) or the old
    // "any 422 with the address in the body" rule kept.
    const src = readFileSync(new URL("../app/[locale]/(console)/settings/settings.tsx", import.meta.url), "utf8");
    assert.match(src, /const refused = refusedKeys\(outcome\.status, outcome\.code\);/);
    assert.match(src, /\} else if \(refused\.length > 0\) \{\s*setInvalid\(refused\);\s*setSaveFailed\(true\);\s*document\.getElementById\(`\$\{formId\}-\$\{refused\[0\]\}`\)\?\.focus\(\);/);
    assert.doesNotMatch(src, /sent\.includes\("contact_email"\)/);
  });
});

describe("ZIF-99 an international domain", () => {
  test("fence: the domain is sent in its ASCII (xn--) form in every browser", () => {
    // Wrong implementation killed: relying on the browser. Chromium's type=email converts the domain, Firefox
    // and Safari do not, so the same address was saved in one browser and refused in another.
    assert.equal(asciiEmail(" Hello@Café.NL "), "hello@xn--caf-dma.nl");
    assert.equal(asciiEmail("info@bücher.de"), "info@xn--bcher-kva.de");
    assert.equal(asciiEmail("hello@xn--caf-dma.nl"), "hello@xn--caf-dma.nl");
    assert.equal(asciiEmail("hello@salonbloom.nl"), "hello@salonbloom.nl");
    assert.equal(asciiEmail(""), "");
    assert.equal(emailProblem("hello@café.nl"), null);
    assert.deepEqual(settingsBody(saved, { ...draftFrom(saved), contact_email: "hello@café.nl" }), {
      body: { contact_email: "hello@xn--caf-dma.nl" },
      invalid: [],
    });
    // The saved xn-- form typed with its accents is no change.
    const withIdn = { ...saved, contact_email: "hello@xn--caf-dma.nl" };
    assert.deepEqual(changedKeys(withIdn, { ...draftFrom(withIdn), contact_email: "hello@café.nl" }), []);
  });

  test("fence: only the domain is converted; an accented name before the @ is still refused", () => {
    // Wrong implementation killed: converting the whole address, or the local part, which has no ASCII form.
    assert.equal(asciiEmail("josé@café.nl"), "josé@xn--caf-dma.nl");
    assert.equal(emailProblem("josé@café.nl"), "accents");
  });

  test("fence: a domain the URL parser would rewrite is left alone and refused", () => {
    // Wrong implementation killed: handing the URL parser a domain it silently changes (it drops tabs and
    // line breaks, and reads /, ?, #, % and : as URL syntax), which would save a different address.
    for (const text of ["a@caf\né.nl", "a@caf\té.nl", "a@café.nl/x", "a@café.nl?x", "a@café.nl#x", "a@café%20.nl", "a@café.nl:80", "a@ café.nl"]) {
      assert.equal(asciiEmail(text), text.trim().toLowerCase(), JSON.stringify(text));
      assert.notEqual(emailProblem(text), null, JSON.stringify(text));
    }
  });
});
