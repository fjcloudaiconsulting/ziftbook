// Logic behind the accept-invite page: which links are worth looking up, which form to show, what an answer means.
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { acceptOutcome, inviteScreen, isInviteToken, firstStage, openedLink } from "../lib/invite.ts";

const TENANT = "0192f3a4-5b6c-7d8e-9f01-23456789abcd";
const SECRET = "Ab3_-".repeat(8) + "xyz"; // 43 URL-safe characters

describe("invite link token", () => {
  test("accepts what the email mints", () => {
    assert.equal(isInviteToken(`${TENANT}.${SECRET}`), true);
    assert.equal(isInviteToken(`${TENANT.toUpperCase()}.${SECRET}`), true);
  });

  test("rejects a secret that is cut short, too long or has characters the email never uses", () => {
    assert.equal(isInviteToken(`${TENANT}.${SECRET.slice(1)}`), false);
    assert.equal(isInviteToken(`${TENANT}.${SECRET}a`), false);
    assert.equal(isInviteToken(`${TENANT}.${SECRET.slice(1)}=`), false);
    assert.equal(isInviteToken(`${TENANT}.${SECRET.slice(1)}%`), false);
  });

  test("rejects a missing or malformed business id, and extra text around the token", () => {
    assert.equal(isInviteToken(SECRET), false);
    assert.equal(isInviteToken(`.${SECRET}`), false);
    assert.equal(isInviteToken(`${TENANT.slice(1)}.${SECRET}`), false);
    assert.equal(isInviteToken(`${TENANT}${SECRET}`), false);
    assert.equal(isInviteToken(`x${TENANT}.${SECRET}`), false);
    assert.equal(isInviteToken(`${TENANT}.${SECRET}\n`), false);
    assert.equal(isInviteToken(`${TENANT}.${SECRET}.${SECRET}`), false);
  });
});

describe("where the page starts", () => {
  test("no link asks for the email link again; a mangled one has expired; a good one is looked up", () => {
    assert.equal(firstStage(null), "missing");
    assert.equal(firstStage("garbage"), "expired");
    assert.equal(firstStage(""), "expired");
    assert.equal(firstStage(`${TENANT}.${SECRET}`), "checking");
  });
});

describe("which screen a lookup leads to", () => {
  test("a live invite asks for a new password or the existing one", () => {
    const details = { business_name: "Nails & Co", email: "a@b.nl" };
    assert.equal(inviteScreen({ status: 200, data: { ...details, has_account: false } }), "new");
    assert.equal(inviteScreen({ status: 200, data: { ...details, has_account: true } }), "existing");
  });

  test("a dead link ends the flow; anything else can be tried again", () => {
    assert.equal(inviteScreen({ status: 400, code: "invalid_token" }), "expired");
    for (const status of [0, 422, 429, 500, 502, 503]) {
      assert.equal(inviteScreen({ status }), "retry", `status ${status}`);
    }
    assert.equal(inviteScreen({ status: 200 }), "retry", "a 200 without a body");
  });
});

describe("what an accept answer means", () => {
  test("each documented answer", () => {
    const cases = [
      [{ status: 201 }, "joined"],
      [{ status: 400, code: "invalid_token" }, "expired"],
      [{ status: 401, code: "invalid_credentials" }, "wrongPassword"],
      [{ status: 409, code: "account_exists" }, "accountExists"],
      [{ status: 409, code: "already_member" }, "alreadyMember"],
      [{ status: 422, code: "password_too_short" }, "password_too_short"],
      [{ status: 422, code: "password_too_long" }, "password_too_long"],
      [{ status: 422, code: "password_too_common" }, "password_too_common"],
      [{ status: 429, code: "rate_limited" }, "tooMany"],
    ];
    for (const [answer, meaning] of cases) assert.equal(acceptOutcome(answer), meaning, JSON.stringify(answer));
  });

  test("anything else is a general problem, never a password or membership message", () => {
    for (const answer of [
      { status: 0 },
      { status: 409 },
      { status: 409, code: "something_new" },
      { status: 422 },
      { status: 422, code: "invalid_request" },
      { status: 503, code: "password_too_short" },
      { status: 500 },
    ]) {
      assert.equal(acceptOutcome(answer), "other", JSON.stringify(answer));
    }
  });
});

describe("opening a link while the page is open", () => {
  const first = { token: "a.1", opened: 0 };

  test("the first link is shown", () => {
    assert.deepEqual(openedLink(null, "a.1", 0), first);
  });

  test("a different link starts over", () => {
    assert.deepEqual(openedLink(first, "b.2", 1), { token: "b.2", opened: 1 });
  });

  test("the same link opened again also starts over", () => {
    const again = openedLink(first, "a.1", 1);
    assert.deepEqual(again, { token: "a.1", opened: 1 });
    assert.notEqual(again, first);
  });

  test("reading the same opening twice changes nothing", () => {
    assert.equal(openedLink(first, "a.1", 0), first);
  });

  test("no fragment keeps what is shown", () => {
    assert.equal(openedLink(first, null, 0), first);
    assert.equal(openedLink(first, null, 3), first);
    assert.equal(openedLink(null, null, 0), null);
  });
});
