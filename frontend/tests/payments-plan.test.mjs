import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

import { createTranslator } from "next-intl";
import { createContext } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import * as jsx from "react/jsx-runtime";
import ts from "typescript";

import * as billing from "../lib/billing.ts";
import { stickyHeight } from "../lib/console.ts";
import * as currency from "../lib/currency.ts";
import * as money from "../lib/money.ts";
import * as settings from "../lib/settings.ts";

const catalog = (locale) => JSON.parse(readFileSync(new URL(`../messages/${locale}.json`, import.meta.url), "utf8"));
const messages = catalog("en");
const uiStyles = { inlineLink: "inlineLink", textButton: "textButton", srOnly: "srOnly" };
const htmlText = (text) => text.replaceAll("'", "&#x27;");
const plan = (enabled) => ({ values: { online_payments: enabled } });

function compile(path) {
  const source = readFileSync(new URL(`../app/[locale]/${path}`, import.meta.url), "utf8");
  const { outputText } = ts.transpileModule(source, {
    fileName: path,
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
  });
  return new Function("require", "exports", outputText);
}

const loadPayments = compile("(console)/settings/payments.tsx");
const loadConsole = compile("_ui/console.tsx");

function featureNote(entitlements, role, locale, messages) {
  const modules = {
    "react/jsx-runtime": jsx,
    react: { createContext, useContext: () => ({ session: { role }, entitlements }) },
    "next-intl": { useTranslations: (namespace) => createTranslator({ locale, messages, namespace }) },
    "@/i18n/navigation": { Link: (props) => jsx.jsx("a", props) },
    "@/lib/billing": billing,
    "@/lib/console": { stickyHeight },
    "./parts": { Banner: ({ children }) => jsx.jsx("div", { role: "status", children }) },
    "./ui.module.css": uiStyles,
  };
  const exports = {};
  loadConsole((name) => modules[name] ?? {}, exports);
  return exports.FeatureNote;
}

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
    "../../_ui/console": { useConsole: () => ({ session: { role: "owner" }, entitlements }), FeatureNote: featureNote(entitlements, "owner", "en", messages) },
    "../../_ui/console.module.css": { body: "body" },
    "../../_ui/parts": {},
    "../../_ui/stripe-dashboard": {},
    "../../_ui/ui.module.css": uiStyles,
  };
  const exports = {};
  loadPayments((name) => modules[name], exports);
  return renderToStaticMarkup(exports.Payments({ disabled: false, switchRow: () => null }));
}

describe("plan-limit copy", () => {
  const expected = {
    en: ["Your plan doesn't include {feature}.", "The business's plan doesn't include {feature}. Ask the business owner."],
    nl: ["Dit zit niet in je abonnement: {feature}.", "Dit zit niet in het abonnement van het bedrijf: {feature}. Vraag het aan de eigenaar van het bedrijf."],
    pt: ["Seu plano não inclui {feature}.", "O plano do negócio não inclui {feature}. Fale com o dono do negócio."],
  };
  for (const [locale, [owner, worker]] of Object.entries(expected)) {
    test(`${locale} renders the owner and worker plan-limit sentences and the owner upgrade link`, () => {
      const messages = catalog(locale);
      const t = createTranslator({ locale, messages, namespace: "Console.planLimit" });
      const entitlements = plan(false);
      const OwnerNote = featureNote(entitlements, "owner", locale, messages);
      const WorkerNote = featureNote(entitlements, "worker", locale, messages);
      for (const key of ["online_payments", "custom_domain"]) {
        const feature = messages.Console.features[key];
        const upgrade = `<a class="inlineLink" href="/settings?tab=plan">${htmlText(t("link"))}</a>`;
        assert.equal(renderToStaticMarkup(jsx.jsx(OwnerNote, { feature: key })), `<div role="status">${htmlText(owner.replace("{feature}", feature))} ${upgrade}</div>`);
        assert.equal(renderToStaticMarkup(jsx.jsx(WorkerNote, { feature: key })), `<div role="status">${htmlText(worker.replace("{feature}", feature))}</div>`);
      }
      for (const feature of Object.values(messages.Console.features)) {
        assert.match(t("owner", { feature }), /^\p{Lu}.*\.$/u, feature);
        assert.match(t("worker", { feature }), /^\p{Lu}.*\.$/u, feature);
      }
    });
  }
});

const accountStatuses = {
  none: ["paymentsNone", "You haven't connected Stripe yet."],
  onboarding: ["paymentsOnboarding", "Stripe setup is incomplete."],
  ready: ["paymentsReady", "Your Stripe account is connected."],
};
const statusText = (key) => htmlText(messages.Console.settings[key]);
const withoutPayments = "Online payments stay off until your plan includes them.";
const unavailableNote = messages.Console.planLimit.unavailable;

for (const [state, [includedKey, accountOnly]] of Object.entries(accountStatuses)) {
  test(`${state} account status follows the plan, including unresolved and unavailable reads`, () => {
    const included = messages.Console.settings[includedKey];
    const excluded = `${accountOnly} ${withoutPayments}`;
    for (const [entitlements, expected] of [[plan(true), included], [plan(false), excluded], [undefined], [null, accountOnly]]) {
      const rendered = renderPayments(state, entitlements);
      const shown = [included, excluded, accountOnly].filter((status) => rendered.includes(`<p class="body">${htmlText(status)}</p>`));
      assert.deepEqual(shown, expected ? [expected] : []);
      assert.equal(rendered.includes(`<div role="status">${htmlText(unavailableNote)}</div>`), entitlements === null);
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
  assert.match(rendered, /<p class="body"><span>Stripe charges its processing fee to your own Stripe account\. <a class="inlineLink" href="https:\/\/stripe\.com\/nl\/pricing" target="_blank" rel="noopener noreferrer">Stripe&#x27;s pricing/);
});
