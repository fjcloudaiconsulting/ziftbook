import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

import { createTranslator } from "next-intl";
import { renderToStaticMarkup } from "react-dom/server";
import * as jsx from "react/jsx-runtime";
import ts from "typescript";

import * as billing from "../lib/billing.ts";
import * as currency from "../lib/currency.ts";
import * as money from "../lib/money.ts";
import * as settings from "../lib/settings.ts";

const catalog = (locale) => JSON.parse(readFileSync(new URL(`../messages/${locale}.json`, import.meta.url), "utf8"));
const source = readFileSync(new URL("../app/[locale]/(console)/settings/payments.tsx", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  fileName: "payments.tsx",
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
});
const loadPayments = new Function("require", "exports", outputText);
const messages = catalog("en");

function renderPayments(state, entitlements, overrides = {}) {
  const account = {
    state, country_supported: true, currency_supported: true, country: "NL", currency: "EUR",
    min_amount_minor: 50, platform_fee_direct: 0, platform_fee_marketplace: 0, ...overrides,
  };
  const modules = {
    "react/jsx-runtime": jsx,
    react: {
      useEffect() {},
      useRef: (current) => ({ current }),
      useState(initial) {
        if (initial === null) return [account];
        return [typeof initial === "function" ? null : initial];
      },
    },
    "next-intl": { useLocale: () => "en", useTranslations: (namespace) => createTranslator({ locale: "en", messages, namespace }) },
    "@/api-client": {},
    "@/lib/billing": billing,
    "@/lib/currency": currency,
    "@/lib/money": money,
    "@/lib/settings": settings,
    "../../_ui/console": { useConsole: () => ({ session: { role: "owner" }, entitlements }), FeatureNote: () => null },
    "../../_ui/console.module.css": { body: "body" },
    "../../_ui/parts": {},
    "../../_ui/stripe-dashboard": {},
    "../../_ui/ui.module.css": { textButton: "textButton", srOnly: "srOnly" },
  };
  const exports = {};
  loadPayments((name) => modules[name], exports);
  return renderToStaticMarkup(exports.Payments({ disabled: false, switchRow: () => null }));
}

describe("plan-limit copy", () => {
  const expected = {
    en: ["Your plan doesn't include online payments.", "Your plan doesn't include a custom domain.", "Ask the business owner."],
    nl: ["Je abonnement omvat online betalingen niet.", "Je abonnement omvat een eigen domein niet.", "Vraag het aan de eigenaar van het bedrijf."],
    pt: ["Seu plano não inclui pagamentos online.", "Seu plano não inclui um domínio próprio.", "Fale com o dono do negócio."],
  };
  for (const [locale, [online, domain, ownerPrompt]] of Object.entries(expected)) {
    test(`${locale} starts the sentence before the feature, including labels with articles`, () => {
      const messages = catalog(locale);
      const t = createTranslator({ locale, messages, namespace: "Console.planLimit" });
      for (const [key, sentence] of [["online_payments", online], ["custom_domain", domain]]) {
        const feature = messages.Console.features[key];
        assert.equal(t("owner", { feature }), sentence);
        assert.equal(t("worker", { feature }), `${locale === "pt" ? sentence.replace("Seu plano", "O plano") : sentence} ${ownerPrompt}`);
      }
      for (const feature of Object.values(messages.Console.features)) {
        assert.match(t("owner", { feature }), /^\p{Lu}.*\.$/u, feature);
        assert.match(t("worker", { feature }), /^\p{Lu}.*\.$/u, feature);
      }
    });
  }
});

const plan = (enabled) => ({ values: { online_payments: enabled } });
const accountStatuses = {
  none: ["paymentsNone", "paymentsPlanNone"],
  onboarding: ["paymentsOnboarding", "paymentsPlanOnboarding"],
  ready: ["paymentsReady", "paymentsPlanReady"],
};
const statusText = (key) => messages.Console.settings[key].replaceAll("'", "&#x27;");

for (const [state, [included, excluded]] of Object.entries(accountStatuses)) {
  test(`${state} account status follows the plan, including unresolved and unavailable reads`, () => {
    for (const [entitlements, expected] of [[plan(true), included], [plan(false), excluded], [undefined], [null]]) {
      const rendered = renderPayments(state, entitlements);
      const shown = Object.values(accountStatuses).flat().filter((key) => rendered.includes(statusText(key)));
      assert.deepEqual(shown, expected ? [expected] : []);
    }
  });
}

test("provider and country restrictions still explain why payments are off", () => {
  assert.ok(renderPayments("off", plan(false)).includes(statusText("paymentsOff")));
  assert.ok(renderPayments("ready", plan(true), { country_supported: false }).includes(statusText("paymentsCountry")));
  assert.match(renderPayments("none", plan(true), { currency_supported: false }), /Online payments aren&#x27;t available in Euro/);
});

test("Stripe pricing has the console text-link class inside the fee sentence", () => {
  const rendered = renderPayments("ready", plan(false));
  assert.match(rendered, /<p class="body"><span>Stripe charges its processing fee to your own Stripe account\. <a class="textButton" href="https:\/\/stripe\.com\/nl\/pricing" target="_blank" rel="noopener noreferrer">Stripe&#x27;s pricing/);
});
