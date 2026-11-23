// The plan and billing screens' pure rules (ZIF-79).
import type { EntitlementsOut, PlanOption } from "@/api-client";

import { formatMoney } from "./money.ts";

export type FeatureAccess = Pick<EntitlementsOut, "values"> | null;

/** Unknown plan details never enable a gated control. Counts are not feature flags. */
export function hasFeature(entitlements: FeatureAccess, feature: string): boolean {
  return entitlements?.values[feature] === true;
}

/** Whole days until `until`, rounded up, never negative. */
export function daysLeft(until: string, now: Date): number {
  return Math.max(0, Math.ceil((new Date(until).getTime() - now.getTime()) / 86_400_000));
}

type Dates = Pick<EntitlementsOut, "source" | "trial_ends_at" | "grant_ends_at" | "grace_ends_at">;

/** The console banner: the downgrade grace beats the trial beats a grant that ends. The trial and grant banners
 * follow where the plan comes from (a business that subscribed during its trial, or was granted a plan, is not
 * on the trial). A date already past shows nothing (the API moves on at its next read). */
export function bannerFor(e: Dates, now: Date): { kind: "trial" | "grace" | "granted" | null; days: number } {
  const picks = [
    ["grace", e.grace_ends_at],
    ["trial", e.source === "trial" ? e.trial_ends_at : null],
    ["granted", e.source === "grant" ? e.grant_ends_at : null],
  ] as const;
  for (const [kind, until] of picks) {
    if (until && new Date(until) > now) return { kind, days: daysLeft(until, now) };
  }
  return { kind: null, days: 0 };
}

/** Whether the owner gets "Manage billing": any Stripe subscription, live or not (unpaid or paused ones give no
 * plan but the portal settles them; an ended one still has its invoices), or one the API just said exists (409
 * already_subscribed). */
export function canManage(e: Pick<EntitlementsOut, "subscription_status">, subscribed: boolean): boolean {
  return e.subscription_status !== null || subscribed;
}

// The statuses of a subscription that is over (backend app/billing.py ENDED): Checkout is open again.
const ENDED = new Set(["canceled", "incomplete_expired"]);

/** Whether Stripe still bills or settles a subscription of the business, so a second Checkout is refused. */
export function stillBilled(e: Pick<EntitlementsOut, "subscription_status">, subscribed: boolean): boolean {
  return subscribed || (e.subscription_status !== null && !ENDED.has(e.subscription_status));
}

/** Whether a plan in the list gets "Subscribe": any but the one in force, and that one too while it is only the
 * trial's (subscribing keeps the trial's days). */
export function canSubscribeTo(e: Pick<EntitlementsOut, "source" | "plan_id">, planId: string): boolean {
  return planId !== e.plan_id || e.source === "trial";
}

type Priced = Pick<
  PlanOption,
  "unit_amount" | "currency" | "interval" | "coupon_id" | "percent_off" | "amount_off" | "coupon_duration" | "coupon_duration_in_months" | "coupon_valid"
>;
export type PriceKey = "perMonth" | "perYear" | "priceOnly" | "onceMonth" | "onceYear" | "repeatingMonth" | "repeatingYear";

/** The discounted amount, or null when the screen can't word one: no price, no known discount, a dead coupon,
 * or terms that don't fit the price (a repeating coupon's months that make no whole years of a yearly price).
 * Stripe rounds a percentage's discount to the minor unit; an amount off never goes below zero. */
function discounted(o: Priced): number | null {
  if (o.unit_amount === null || o.coupon_valid === false) return null;
  const unit = o.interval === "month" || o.interval === "year";
  if (o.coupon_duration === "once" && !unit) return null;
  if (o.coupon_duration === "repeating") {
    const months = o.coupon_duration_in_months ?? 0;
    if (!unit || months < 1 || (o.interval === "year" && months % 12 !== 0)) return null;
  }
  if (o.percent_off != null) return o.unit_amount - Math.round((o.unit_amount * o.percent_off) / 100);
  if (o.amount_off != null) return Math.max(0, o.unit_amount - o.amount_off);
  return null;
}

/** The plan's price as a message (its key in Console.billing and values), or null when it has none. A coupon
 * the API knows is worded as Stripe applies it: forever is the discounted price; once is the first month or year
 * at the discounted price, then the list price; repeating is the discounted price for its months (whole years on
 * a yearly price), then the list price. Otherwise the list price, and priceNote says why. */
export function priceLabel(option: Priced, locale: string): { key: PriceKey; values: { price: string; then?: string; count?: number } } | null {
  if (option.unit_amount === null || !option.currency) return null;
  // Stripe sends the code in lower case ("eur"); Intl lists it in upper.
  const money = (amount: number) => formatMoney(amount, option.currency!.toUpperCase(), locale);
  const yearly = option.interval === "year";
  const list = money(option.unit_amount);
  const off = discounted(option);
  if (off === null || option.coupon_duration === "forever" || option.coupon_duration === null)
    return { key: option.interval === "month" ? "perMonth" : yearly ? "perYear" : "priceOnly", values: { price: off === null ? list : money(off) } };
  if (option.coupon_duration === "once") return { key: yearly ? "onceYear" : "onceMonth", values: { price: money(off), then: list } };
  const months = option.coupon_duration_in_months!;
  return { key: yearly ? "repeatingYear" : "repeatingMonth", values: { price: money(off), then: list, count: yearly ? months / 12 : months } };
}

/** The note under a plan's price: "couponGone" once Stripe says the offer's coupon is deleted, expired or used up;
 * "coupon" (discount applied at checkout) for a coupon whose discount the screen can't word; else none. */
export function priceNote(option: Priced): "coupon" | "couponGone" | null {
  if (option.coupon_valid === false) return "couponGone";
  return option.coupon_id && discounted(option) === null ? "coupon" : null;
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
  | "couponUnavailable"
  | "failed" {
  if (outcome.status === 503 && outcome.code === "billing_unavailable") return "unavailable";
  if (outcome.status === 409) {
    if (outcome.code === "already_subscribed") return "alreadySubscribed";
    if (outcome.code === "plan_granted") return "granted";
    if (outcome.code === "not_purchasable") return "notPurchasable";
    if (outcome.code === "no_subscription") return "noSubscription";
    if (outcome.code === "coupon_unavailable") return "couponUnavailable";
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
