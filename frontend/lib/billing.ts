// The plan and billing screens' pure rules (ZIF-79).
import type { EntitlementsOut, PlanOption } from "@/api-client";

import { formatMoney } from "./money.ts";

/** Whole days until `until`, rounded up, never negative. */
export function daysLeft(until: string, now: Date): number {
  return Math.max(0, Math.ceil((new Date(until).getTime() - now.getTime()) / 86_400_000));
}

type Dates = Pick<EntitlementsOut, "source" | "trial_ends_at" | "grant_ends_at" | "grace_ends_at">;

/** The console banner: the downgrade grace beats the trial beats a grant that ends. A date already past
 * shows nothing (the API moves on at its next read). */
export function bannerFor(e: Dates, now: Date): { kind: "trial" | "grace" | "granted" | null; days: number } {
  const picks = [
    ["grace", e.grace_ends_at],
    ["trial", e.trial_ends_at],
    ["granted", e.grant_ends_at],
  ] as const;
  for (const [kind, until] of picks) {
    if (until && new Date(until) > now) return { kind, days: daysLeft(until, now) };
  }
  return { kind: null, days: 0 };
}

/** The plan's price in the reader's format, or "" when it has none. A coupon changes nothing here: the
 * API carries no discounted amount, so the screen notes the discount instead of computing one. */
export function priceLabel(option: Pick<PlanOption, "unit_amount" | "currency" | "coupon_id">, locale: string): string {
  if (option.unit_amount === null || !option.currency) return "";
  return formatMoney(option.unit_amount, option.currency, locale);
}

/** A fee held in basis points, as a percentage ("0%" stated, never left out). */
export function feeLabel(basisPoints: number, locale: string): string {
  return new Intl.NumberFormat(locale, { style: "percent", maximumFractionDigits: 2 }).format(basisPoints / 10_000);
}

/** The message key for a refused checkout or portal call. */
export function billingError(outcome: { status: number; code?: string }):
  | "unavailable"
  | "alreadySubscribed"
  | "granted"
  | "notPurchasable"
  | "noSubscription"
  | "notFound"
  | "failed" {
  if (outcome.status === 503 && outcome.code === "billing_unavailable") return "unavailable";
  if (outcome.status === 409) {
    if (outcome.code === "already_subscribed") return "alreadySubscribed";
    if (outcome.code === "plan_granted") return "granted";
    if (outcome.code === "not_purchasable") return "notPurchasable";
    if (outcome.code === "no_subscription") return "noSubscription";
  }
  if (outcome.status === 404 && outcome.code === "plan_not_found") return "notFound";
  return "failed";
}

/** Whether a URL the API sent is Stripe's own Checkout or customer portal: the only places the console
 * hands the browser to (never a script, data or plain-http address, nor a look-alike host). */
export function isStripeUrl(url: string): boolean {
  try {
    const u = new URL(url);
    return u.protocol === "https:" && (u.hostname === "checkout.stripe.com" || u.hostname === "billing.stripe.com");
  } catch {
    return false;
  }
}
