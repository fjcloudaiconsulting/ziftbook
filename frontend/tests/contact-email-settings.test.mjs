// ZIF-232: what the Settings block says about the contact email, for each state the API answers.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

import { contactEmailView, contactSendOutcome } from "../lib/settings.ts";

const none = { state: "none", confirmed: null, requested: null, link_expired: false, expires_at: null };
const unconfirmed = { ...none, state: "unconfirmed", requested: "new@x.nl" };
const expired = { ...unconfirmed, link_expired: true, expires_at: "2026-11-02T10:00:00Z" };
const sent = { ...unconfirmed, state: "sent", expires_at: "2026-11-04T10:00:00Z" };
const confirmed = { ...none, state: "confirmed", confirmed: "shop@x.nl" };
const sentWhileConfirmed = { ...sent, confirmed: "shop@x.nl" };

// F8.1 FENCE. Wrong impl: a waiting address shown as the one clients see, or the wrong action.
describe("F8.1 each state has its pill, words and action", () => {
  test("an owner", () => {
    assert.equal(contactEmailView(none, true), null);
    assert.deepEqual(contactEmailView(unconfirmed, true), {
      pill: "unconfirmed",
      body: ["contactUnconfirmed", { email: "new@x.nl" }],
      untilThen: null,
      action: "contactSend",
    });
    assert.deepEqual(contactEmailView(expired, true), {
      pill: "unconfirmed",
      body: ["contactExpired", { email: "new@x.nl" }],
      untilThen: null,
      action: "contactSendNew",
    });
    assert.deepEqual(contactEmailView(sent, true), {
      pill: "sent",
      body: ["contactSent", { email: "new@x.nl" }],
      untilThen: ["contactUntilThenNone", {}],
      action: "contactSendAgain",
    });
    assert.deepEqual(contactEmailView(confirmed, true), {
      pill: "confirmed",
      body: ["contactConfirmed", { email: "shop@x.nl" }],
      untilThen: null,
      action: null,
    });
    assert.deepEqual(contactEmailView(sentWhileConfirmed, true).untilThen, ["contactUntilThen", { email: "shop@x.nl" }]);
    assert.deepEqual(contactEmailView({ ...expired, confirmed: "shop@x.nl" }, true).untilThen, [
      "contactUntilThen",
      { email: "shop@x.nl" },
    ]);
  });

  // F8.2 FENCE. Wrong impl: a send button for a worker member, or words that need the waiting
  // address the API does not give them.
  test("a worker member gets no button and no waiting address", () => {
    for (const state of [unconfirmed, expired, sent]) {
      const view = contactEmailView({ ...state, requested: null }, false);
      assert.equal(view.action, null);
      assert.deepEqual(view.body, ["contactWaiting", {}]);
      assert.equal(view.ownerOnly, true);
    }
    assert.deepEqual(contactEmailView(confirmed, false), {
      pill: "confirmed",
      body: ["contactConfirmed", { email: "shop@x.nl" }],
      untilThen: null,
      action: null,
    });
  });
});

// F8.3 FENCE. Wrong impl: a silent failure, or one message for every refusal (an owner kept from an
// address for the day by other businesses must be told that, not "try later").
describe("F8.3 what a send answer means", () => {
  test("each answer has its words", () => {
    assert.equal(contactSendOutcome(200), "sent");
    assert.equal(contactSendOutcome(429, "rate_limited"), "contactTooMany");
    assert.equal(contactSendOutcome(429, "contact_email_requests_today"), "contactTooManyToday");
    assert.equal(contactSendOutcome(401), "signedOut");
    assert.equal(contactSendOutcome(409, "no_contact_email_request"), "reload");
    for (const status of [0, 403, 422, 500, 503]) assert.equal(contactSendOutcome(status), "contactFailed");
  });
});

describe("F8.3 the copy", () => {
  const keys = [
    "contactEmailLabel", "contactUnconfirmedPill", "contactSentPill", "contactConfirmedPill", "contactUnconfirmed",
    "contactExpired", "contactSent", "contactConfirmed", "contactUntilThen", "contactUntilThenNone", "contactWaiting",
    "contactSend", "contactSendNew", "contactSendAgain", "contactLinkSent", "contactTooMany", "contactTooManyToday",
    "contactFailed",
    "contactLoadFailed", "contactOwnerOnly",
  ];
  for (const locale of ["en", "nl", "pt"]) {
    test(`${locale} has every key and no em dash`, () => {
      const copy = JSON.parse(readFileSync(new URL(`../messages/${locale}.json`, import.meta.url), "utf8")).Console.settings;
      for (const key of keys) {
        assert.equal(typeof copy[key], "string", key);
        assert.doesNotMatch(copy[key], /—/, key);
      }
    });
  }
});
