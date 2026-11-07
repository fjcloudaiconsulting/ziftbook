// Logic behind the admin sign-in screens: routing on the session, the manual key, the recovery-codes file and the
// error-code to state mapping.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

import { appCode, enrolCommand, factorState, recoveryFile, secretGroups, shellQuote, stepFor } from "../lib/admin.ts";

describe("routing on second_factor", () => {
  // FENCE routing: a half session must never land in the console.
  test("only a passed second factor leaves sign-in", () => {
    assert.equal(stepFor("passed"), "done");
    assert.equal(stepFor("required"), "code");
    assert.equal(stepFor("enrolment_required"), "ticket");
  });

  // GUARD: a value this build doesn't know is not a pass.
  test("an unknown value asks for a code", () => {
    assert.equal(stepFor("something_new"), "code");
  });
});

describe("manual key", () => {
  test("groups of four, the last one short", () => {
    assert.deepEqual(secretGroups("ABCDEFGHIJ"), ["ABCD", "EFGH", "IJ"]);
    assert.deepEqual(secretGroups(""), []);
  });
});

describe("recovery codes file", () => {
  // FENCE file: codes are numbered from 1, one per line, with a trailing newline.
  test("header, blank line, numbered codes", () => {
    assert.equal(recoveryFile("head", ["AAAA", "BBBB"]), "head\n\n1. AAAA\n2. BBBB\n");
  });
});

describe("answer to state", () => {
  // FENCE codes: the spec's section 1 table.
  test("codes map to their states whatever the status", () => {
    assert.equal(factorState(401, "invalid_code"), "wrong");
    assert.equal(factorState(401, "invalid_ticket"), "wrong");
    assert.equal(factorState(403, "second_factor_locked"), "locked");
    assert.equal(factorState(503, "second_factor_unavailable"), "unavailable");
  });

  // FENCE session: a 401 without a wrong-code code means the session is gone.
  test("a bare 401 is an expired session", () => {
    assert.equal(factorState(401, "unauthenticated"), "expired");
    assert.equal(factorState(401), "expired");
  });

  test("429, 409 and the rest", () => {
    assert.equal(factorState(429, "rate_limited"), "tooMany");
    assert.equal(factorState(409, "enrolment_required"), "conflict");
    assert.equal(factorState(409, "already_enrolled"), "conflict");
    assert.equal(factorState(409, "no_pending_enrolment"), "conflict");
    assert.equal(factorState(503), "other");
    assert.equal(factorState(0), "other");
  });
});

describe("app code", () => {
  // FENCE appcode: six digits, whitespace stripped; seven digits or letters are refused.
  test("accepts 6 digits with spaces, refuses anything else", () => {
    assert.equal(appCode("123456"), "123456");
    assert.equal(appCode(" 123 456 "), "123456");
    assert.equal(appCode("1234567"), null);
    assert.equal(appCode("12345"), null);
    assert.equal(appCode("12345a"), null);
  });
});

describe("enrol command", () => {
  // FENCE quoting: the backend's email regex lets ' & | $ and backticks through, so the email is always quoted.
  test("a plain email is still single-quoted", () => {
    assert.equal(enrolCommand("a@b.co"), "python -m app.admin enrol 'a@b.co'");
  });

  test("a quote is closed, escaped and reopened; the rest stays inside the quotes", () => {
    assert.equal(shellQuote("o'x`id`$HOME&|@b.co"), "'o'\\''x`id`$HOME&|@b.co'");
  });
});

describe("shell quoting edge cases", () => {
  // FENCE quoting-edge: an empty value is still an argument, and a newline stays inside the quotes.
  test("empty and multi-line values", () => {
    assert.equal(shellQuote(""), "''");
    assert.equal(shellQuote("a\nb"), "'a\nb'");
  });
});

describe("a stopped setup", () => {
  // ZIF-262: entering the same ticket again keeps the wrong-code count, so the way out is a new ticket from the
  // command, in every language.
  const expected = {
    en: {
      enrolLocked:
        "Too many wrong codes were entered, so this setup stopped, and this ticket can't start it again. Ask for a new ticket with:",
      newTicket: "Enter a new ticket",
    },
    nl: {
      enrolLocked:
        "Er zijn te veel verkeerde codes ingevuld, dus het instellen is gestopt, en met dit ticket begin je niet opnieuw. Vraag een nieuw ticket aan met:",
      newTicket: "Nieuw ticket invullen",
    },
    pt: {
      enrolLocked:
        "Foram digitados códigos errados demais, então a configuração foi interrompida, e este ticket não serve para recomeçar. Peça um ticket novo com:",
      newTicket: "Digitar um ticket novo",
    },
  };
  for (const [locale, texts] of Object.entries(expected)) {
    test(`asks for a new ticket (${locale})`, () => {
      const { Admin } = JSON.parse(readFileSync(new URL(`../messages/${locale}.json`, import.meta.url), "utf8"));
      assert.deepEqual({ enrolLocked: Admin.enrolLocked, newTicket: Admin.newTicket }, texts);
    });
  }
});

describe("a ticket entered after a stopped setup", () => {
  // node --test cannot render these components (no DOM or React renderer here, as today.test.mjs notes), so these
  // check the answer mapping and, loosely, which step uses what.
  const enrol = readFileSync(new URL("../app/[locale]/admin/sign-in/enrol.tsx", import.meta.url), "utf8");
  const ticketStep = enrol.slice(enrol.indexOf("export function TicketStep"), enrol.indexOf("function SetupStopped"));
  const scanStep = enrol.slice(enrol.indexOf("export function ScanStep"), enrol.indexOf("export function RecoveryCodes"));
  // ZIF-275 fence. Kills: a stopped enrolment's ticket (403 second_factor_locked from begin, also after a reload)
  // shown as a wrong ticket or "something went wrong" instead of the "Setup stopped" screen the confirm step shows.
  test("a stopped enrolment's ticket shows the Setup stopped screen, not a QR code", () => {
    assert.equal(factorState(403, "second_factor_locked"), "locked");
    assert.match(ticketStep, /state === "locked"/);
    assert.match(ticketStep, /<SetupStopped/);
    assert.match(scanStep, /<SetupStopped/);
  });
  // ZIF-262: back on the ticket form from that screen, the admin still reads why and what to do, not only "that
  // ticket doesn't match", and a refused ticket then keeps only that text.
  test("the stopped screen takes its text to the ticket form, marked to stay", () => {
    for (const step of [ticketStep, scanStep]) assert.match(step, /text: t\("enrolLocked"\), sticky: true/);
    assert.match(ticketStep, /state === "wrong"\) \{\s*setMessage\(sticky\);/);
  });
});
