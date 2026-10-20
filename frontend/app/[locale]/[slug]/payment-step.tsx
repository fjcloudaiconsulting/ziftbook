"use client";

import { loadStripe } from "@stripe/stripe-js/pure";
import type { Appearance, Stripe, StripeElements, StripePaymentElement } from "@stripe/stripe-js";
import type { useTranslations } from "next-intl";
import { type Ref, useEffect, useImperativeHandle, useMemo, useRef } from "react";

import type { PagePaymentOut } from "@/api-client";
import { elementsOptions, type PaymentChoice, STRIPE_LOCALE } from "@/lib/booking-page";
import { formatMoney } from "@/lib/money";

import styles from "../_ui/ui.module.css";

export type PayHandle = {
  /** Checks the Element's fields; the message to show, or null. */
  submit(): Promise<string | null>;
  /** Confirms the intent; the message to show, or null when it went through (or the browser left for the bank). */
  confirm(start: { client_secret: string; type: "payment" | "setup" }, returnUrl: string): Promise<string | null>;
};

/** A token colour as sRGB: Stripe's appearance does not read oklch(), a canvas does. */
function rgb(value: string): string {
  const ctx = document.createElement("canvas").getContext("2d", { willReadFrequently: true });
  if (!ctx) return "#000000";
  ctx.clearRect(0, 0, 1, 1);
  ctx.fillStyle = value.trim();
  ctx.fillRect(0, 0, 1, 1);
  const [r, g, b] = ctx.getImageData(0, 0, 1, 1).data;
  return `rgb(${r}, ${g}, ${b})`;
}

/** The Element drawn with the app's tokens (no Stripe default blue). */
function appearance(): Appearance {
  const root = getComputedStyle(document.documentElement);
  const token = (name: string) => rgb(root.getPropertyValue(name));
  const primary = token("--primary");
  return {
    variables: {
      colorPrimary: primary,
      colorText: token("--ink"),
      colorDanger: token("--error"),
      colorBackground: token("--bg"),
      borderRadius: "0.75rem",
      fontFamily: getComputedStyle(document.body).fontFamily,
    },
    rules: { ".Input:focus": { borderColor: primary, boxShadow: `0 0 0 2px ${primary}`, outline: "none" } },
  };
}

/** The check / pay choice and, under it, Stripe's Payment Element mounted in a ref (as Turnstile is). */
export function PaymentStep({
  ref,
  payment,
  price,
  locale,
  biz,
  choice,
  busy,
  legend,
  t,
  onChoice,
  onUnavailable,
}: {
  ref: Ref<PayHandle>;
  payment: PagePaymentOut;
  price: { amount_minor: number; currency: string };
  locale: string;
  biz: string;
  choice: PaymentChoice;
  busy: boolean;
  /** false: the surrounding section's heading already names the group. */
  legend: boolean;
  t: ReturnType<typeof useTranslations>;
  onChoice(choice: PaymentChoice): void;
  onUnavailable(): void;
}) {
  const box = useRef<HTMLDivElement>(null);
  const live = useRef<{ stripe: Stripe; elements: StripeElements } | null>(null);
  const stripePromise = useMemo(
    () => loadStripe(payment.publishable_key, { stripeAccount: payment.stripe_account, locale: STRIPE_LOCALE[locale] as "en" }),
    [payment.publishable_key, payment.stripe_account, locale],
  );

  // A new Elements per choice, not elements.update: the two kinds differ in capture and in the
  // method list, and update cannot clear a list.
  useEffect(() => {
    let off = false;
    let element: StripePaymentElement | null = null;
    void stripePromise.then(
      (stripe) => {
        if (off) return;
        if (!stripe || !box.current) return onUnavailable();
        const elements = stripe.elements({ ...elementsOptions(choice, payment.check_with, price), appearance: appearance() });
        element = elements.create("payment");
        element.mount(box.current);
        live.current = { stripe, elements };
      },
      () => !off && onUnavailable(),
    );
    return () => {
      off = true;
      element?.destroy();
      live.current = null;
    };
    // The price and the options of one service never change under a mounted step.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stripePromise, choice]);

  useImperativeHandle(ref, () => ({
    async submit() {
      if (!live.current) return t("payUnavailable");
      const { error } = await live.current.elements.submit();
      return error ? (error.message ?? t("payFailed")) : null;
    },
    async confirm(start, returnUrl) {
      if (!live.current) return t("payUnavailable");
      const { stripe, elements } = live.current;
      const args = { elements, clientSecret: start.client_secret, confirmParams: { return_url: returnUrl }, redirect: "if_required" as const };
      const { error } = start.type === "setup" ? await stripe.confirmSetup(args) : await stripe.confirmPayment(args);
      return error ? (error.message ?? t("payFailed")) : null;
    },
  }));

  const amount = formatMoney(price.amount_minor, price.currency, locale);
  return (
    <>
      <fieldset className={styles.choices} aria-labelledby={legend ? undefined : "pay-h"} disabled={busy}>
        {legend && <legend className={`${styles.label} ${styles.payLegend}`}>{t("payTitle")}</legend>}
        <label className={styles.choice}>
          <input type="radio" name="pay" checked={choice === "check"} onChange={() => onChoice("check")} />
          <span>
            <b>{t("payCheck")}</b>
            <em>{t(payment.check_with === "setup" ? "payCheckSetupHint" : "payCheckHint", { price: amount })}</em>
          </span>
        </label>
        <label className={styles.choice}>
          <input type="radio" name="pay" checked={choice === "pay"} onChange={() => onChoice("pay")} />
          <span>
            <b>{t("payNow", { price: amount })}</b>
            <em>{t(payment.keeps_card_fee ? "payNowHintFee" : "payNowHint", { biz })}</em>
          </span>
        </label>
      </fieldset>
      <div ref={box} />
    </>
  );
}
