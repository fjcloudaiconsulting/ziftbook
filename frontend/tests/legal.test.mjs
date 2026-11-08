// The Terms and Privacy pages (ZIF-147): every language has the same shape as English, and none
// ships with an owner fact still unfilled.
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { LEGAL } from "../lib/legal/index.ts";

describe("legal pages", () => {
  test("no [OWNER: ...] placeholder is left in any language", () => {
    const left = Object.entries(LEGAL).flatMap(([locale, docs]) =>
      [...JSON.stringify(docs).matchAll(/\[OWNER:[^\]]*\]/g)].map((m) => `${locale}: ${m[0]}`),
    );
    assert.deepEqual(left, []);
  });

  test("nl and pt have English's sections, in the same order and shape", () => {
    const shape = (docs) =>
      Object.values(docs).map((doc) => doc.sections.map((s) => s.body.map((b) => (Array.isArray(b) ? b.length : "p"))));
    for (const locale of ["nl", "pt"]) assert.deepEqual(shape(LEGAL[locale]), shape(LEGAL.en), locale);
  });

  // GDPR Art. 13(2)(a), ZIF-263: each period the retention sweep (ZIF-235) enforces is stated, next
  // to what it applies to, in every language. Each pair must share one paragraph or bullet.
  const PERIODS = {
    en: [
      ["Sign-in sessions", "no longer valid"],
      ["attempts", "about a day"],
      ["failed sign-ins", "30 days"],
      ["everything else", "1 year"],
      ["IP address and browser type", "18 months"],
      ["offers and news", "as long as your choice stands"],
      ["change your answer", "1 year"], // ZIF-156: the consent's address, once it is replaced
      ["security incident", "90 days"],
    ],
    nl: [
      ["Inlogsessies", "niet meer geldig"],
      ["pogingen", "ongeveer een dag"],
      ["mislukte inlogpogingen", "30 dagen"],
      ["al het andere", "1 jaar"],
      ["IP-adres en browsertype", "18 maanden"],
      ["aanbiedingen en nieuws", "zolang je keuze geldt"],
      ["je antwoord wijzigt", "1 jaar"],
      ["beveiligingsincident", "90 dagen"],
    ],
    pt: [
      ["Sessões de acesso", "deixam de ser válidas"],
      ["tentativas", "cerca de um dia"],
      ["tentativas de acesso que falharam", "30 dias"],
      ["todo o resto", "1 ano"],
      ["endereço IP e o tipo de navegador", "18 meses"],
      ["ofertas e novidades", "enquanto a sua escolha valer"],
      ["mudar a sua resposta", "1 ano"],
      ["incidente de segurança", "90 dias"],
    ],
  };

  test("the privacy notice states how long each kind of data is kept, in every language", () => {
    const at = LEGAL.en.privacy.sections.findIndex((s) => s.heading === "How long we keep data");
    assert.notEqual(at, -1);
    for (const [locale, pairs] of Object.entries(PERIODS)) {
      const blocks = LEGAL[locale].privacy.sections[at].body.flat();
      for (const [what, period] of pairs)
        assert.ok(blocks.some((b) => b.includes(what) && b.includes(period)), `${locale}: ${what} / ${period}`);
    }
  });

  test("no em dash, and no list repeats an item (each is its own React key)", () => {
    assert.ok(!JSON.stringify(LEGAL).includes("—"));
    const lists = Object.values(LEGAL).flatMap((docs) => Object.values(docs).flatMap((doc) => doc.sections.flatMap((s) => s.body.filter(Array.isArray))));
    for (const list of lists) assert.equal(new Set(list).size, list.length, list[0]);
  });
});
