// ZIF-102: the business's currency on the settings screen, as plain logic. The owner changes it only
// until the business's first booking and before it connects Stripe (owner's ruling); the database
// holds that rule (change_currency), and the screen only follows GET /api/settings/currency.

import type { CurrencyState } from "@/api-client";

import { currencySign, formatMoney } from "./money.ts";

/** The currency's name in the reader's language (Intl.DisplayNames: no catalog entries). */
export function currencyName(code: string, locale: string): string {
  return new Intl.DisplayNames([locale], { type: "currency" }).of(code) ?? code;
}

/** "Euro (€)": the name, then the plain symbol the prices show. */
export function currencyLabel(code: string, locale: string): string {
  return `${currencyName(code, locale)} (${currencySign(0, code, locale)})`;
}

type Option = CurrencyState["options"][number];

/** The currencies the business could change to: every other one it can hold, in the API's order. */
export function currencyChoices(state: Pick<CurrencyState, "currency" | "options">): Option[] {
  return state.options.filter((o) => o.code !== state.currency);
}

/** PUT /api/settings/currency: the count of saved prices the owner was told keep their numbers. The
 * API refuses a different count (409 currency_prices_exist), so a price saved meanwhile is never
 * relabelled unseen. */
export function changeBody(currency: Option["code"], prices: number): { currency: Option["code"]; confirmed_prices: number } {
  return { currency, confirmed_prices: prices };
}

/** "€35.00 becomes $35.00": the same minor units in each currency, which is what a change does. */
export function changeExample(from: string, to: string, locale: string): { from: string; to: string } {
  return { from: formatMoney(3500, from, locale), to: formatMoney(3500, to, locale) };
}

/** Whether changing to `next` leaves a currency Stripe pays out in for the business's country for
 * one it does not: connecting Stripe is then refused. */
export function losesPayments(state: Pick<CurrencyState, "currency" | "options">, next: string): boolean {
  const settles = (code: string) => state.options.find((o) => o.code === code)?.stripe_settles ?? false;
  return settles(state.currency) && !settles(next);
}

/** What a refused change means for the screen; null for the generic failure. */
export function currencyChangeError(outcome: { status: number; code?: string }): "signedOut" | "ownerOnly" | "recount" | "locked" | null {
  if (outcome.status === 401) return "signedOut";
  if (outcome.status === 403 && outcome.code === "owner_only") return "ownerOnly";
  if (outcome.status === 409 && outcome.code === "currency_prices_exist") return "recount";
  if (outcome.status === 409 && (outcome.code === "currency_locked_bookings" || outcome.code === "currency_locked_payments")) return "locked";
  return null;
}
