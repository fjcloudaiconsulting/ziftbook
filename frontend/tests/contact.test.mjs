// ZIF-99: the business's contact email on the public booking page and the manage page. The address
// is text the owner typed, so it is only ever text, and the mailto: link can carry nothing else.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

import { mailtoHref } from "../lib/contact.ts";

const read = (rel) => readFileSync(new URL(rel, import.meta.url), "utf8");
const catalog = (locale) => JSON.parse(read(`../messages/${locale}.json`));

describe("mailtoHref", () => {
  test("a plain address is a plain mailto: link", () => {
    assert.equal(mailtoHref("hello@salonbloom.nl"), "mailto:hello@salonbloom.nl");
    assert.equal(mailtoHref("first.last@xn--caf-dma.nl"), "mailto:first.last@xn--caf-dma.nl");
  });

  test("fence: nothing in the address can add a header, a body or a second recipient", () => {
    // Wrong implementation killed: `mailto:${email}` as is, where ?, &, #, % or a line break in the
    // owner's text would start a query (cc=, bcc=, body=) or end the address early.
    const cases = [
      ["a?cc=x@evil.example", "mailto:a%3Fcc%3Dx@evil.example"],
      ["a&bcc=x@b.nl", "mailto:a%26bcc%3Dx@b.nl"],
      ["a#b@c.nl", "mailto:a%23b@c.nl"],
      ["a%0Abcc@c.nl", "mailto:a%250Abcc@c.nl"],
      ["a\r\nBcc: x@y.z@c.nl", "mailto:a%0D%0ABcc%3A%20x%40y.z@c.nl"],
      ["a+shop@c.nl", "mailto:a%2Bshop@c.nl"],
      ["a@c.nl?body=hi", "mailto:a@c.nl%3Fbody%3Dhi"],
    ];
    for (const [address, href] of cases) {
      assert.equal(mailtoHref(address), href, JSON.stringify(address));
      assert.doesNotMatch(href.slice("mailto:".length), /[?&#\s]/, JSON.stringify(address));
    }
  });

  test("an address without @ is still encoded whole", () => {
    assert.equal(mailtoHref("no-at?x"), "mailto:no-at%3Fx");
  });
});

describe("the contact line", () => {
  const line = read("../app/[locale]/_ui/contact-line.tsx");

  test("fence: the address is rendered as text and linked only through mailtoHref", () => {
    // Wrong implementation killed: HTML from the owner's text, or a raw `mailto:${email}`.
    assert.doesNotMatch(line, /dangerouslySetInnerHTML/);
    assert.match(line, /href=\{mailtoHref\(email\)\}/);
    assert.match(line, />\{email\}</);
    assert.doesNotMatch(line, /`mailto:/);
  });

  test("fence: the label is for screen readers only, right before the link", () => {
    // Wrong implementation killed: a plain span, which shows "Contact email:" as visible text on both pages.
    assert.match(line, /<MailIcon \/>\s*<span className=\{styles\.srOnly\}>\{label\}<\/span>\s*<a href=/);
    assert.match(read("../app/[locale]/_ui/ui.module.css"), /^\.srOnly \{/m);
  });

  test("fence: the booking page shows it in the business header only when there is an address", () => {
    // Wrong implementation killed: an empty line (icon, label, empty link) when the setting is empty.
    const page = read("../app/[locale]/[slug]/services-page.tsx");
    assert.match(page, /\{page\.contact_email && <ContactLine email=\{page\.contact_email\} label=\{t\("contactLabel"\)\} \/>\}/);
    assert.doesNotMatch(page, /dangerouslySetInnerHTML/);
  });

  test("every language labels the booking page's line for screen readers", () => {
    assert.deepEqual(
      ["en", "nl", "pt"].map((l) => catalog(l).BookingPage.contactLabel),
      ["Contact email: ", "Contactadres: ", "E-mail de contato: "],
    );
  });
});

describe("the manage page", () => {
  const page = read("../app/[locale]/booking/manage-booking.tsx");

  test("fence: the line sits under the business name only when there is an address", () => {
    // Wrong implementation killed: an empty line when the setting is empty.
    assert.match(page, /const contact = booking\.contact_email;/);
    assert.match(page, /\{contact && <ContactLine email=\{contact\} label=\{t\("contactLabel"\)\} tight \/>\}/);
    assert.doesNotMatch(page, /dangerouslySetInnerHTML/);
  });

  test("fence: a booking that can no longer be changed names the address, linked through mailtoHref", () => {
    // Wrong implementation killed: "Contact {business}." with no way to reach them, or the address
    // spliced into the message as markup.
    assert.match(
      page,
      /\{contact\s*\?\s*t\.rich\("noChangesEmail", \{ business: booking\.business, email: contact, link: \(chunks\) => <a href=\{mailtoHref\(contact\)\}>\{chunks\}<\/a> \}\)\s*:\s*t\("noChanges", \{ business: booking\.business \}\)\}/,
    );
  });

  test("every language has the manage page's words, the address inside the link", () => {
    assert.deepEqual(
      ["en", "nl", "pt"].map((l) => catalog(l).BookingLink.contactLabel),
      ["Contact email: ", "Contactadres: ", "E-mail de contato: "],
    );
    assert.deepEqual(
      ["en", "nl", "pt"].map((l) => catalog(l).BookingLink.noChangesEmail),
      [
        "This booking can no longer be changed online. Contact {business} at <link>{email}</link>.",
        "Deze afspraak kan niet meer online worden gewijzigd. Neem contact op met {business} via <link>{email}</link>.",
        "Este agendamento não pode mais ser alterado on-line. Entre em contato com {business} pelo e-mail <link>{email}</link>.",
      ],
    );
  });
});
