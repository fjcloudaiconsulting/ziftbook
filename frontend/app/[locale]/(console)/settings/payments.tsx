"use client";

import { useLocale, useTranslations } from "next-intl";
import { type ReactNode, useEffect, useRef, useState } from "react";

import { type PaymentAccountOut, paymentsAccount, paymentsConnect } from "@/api-client";
import { hasFeature } from "@/lib/billing";
import { currencyName } from "@/lib/currency";
import { formatMoney } from "@/lib/money";
import { connectError, paymentsAction, percentOf, stripePricingUrl, withoutStripe } from "@/lib/settings";

import { FeatureNote, SignedOutBanner, useConsole } from "../../_ui/console";
import styles from "../../_ui/console.module.css";
import { Banner } from "../../_ui/parts";
import { openStripeDashboard } from "../../_ui/stripe-dashboard";
import uiStyles from "../../_ui/ui.module.css";

/** Online payments: the Stripe connection's state, and the one action it allows (owners only). `switchRow`
 * is the settings form's own checkbox that turns the checkout's payment step on (it saves with the page). */
export function Payments({ disabled, switchRow }: { disabled: boolean; switchRow(min: string, ready: boolean): ReactNode }) {
  const { session, call, entitlements } = useConsole();
  const t = useTranslations("Console.settings");
  const locale = useLocale() as "en" | "nl" | "pt";
  const [account, setAccount] = useState<PaymentAccountOut | null>(null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const [currencyChanged, setCurrencyChanged] = useState(false);
  const [signedOut, setSignedOut] = useState(false);
  const busyRef = useRef(false);
  const owner = session.role === "owner";
  const available = hasFeature(entitlements, "online_payments");
  const planLoading = entitlements === undefined;

  // Stripe sends the owner back with ?stripe=return (read the state again) or ?stripe=refresh (the
  // link expired: fetch a fresh one); either way the param goes once handled. Read at mount, before the
  // effect drops it, so a second run of the effect still knows.
  const [back] = useState(() => new URLSearchParams(window.location.search).get("stripe"));
  useEffect(() => {
    if (planLoading) return;
    if (back) {
      // Only `stripe` goes: the tab stays the one the address opened (Settings owns ?tab).
      const url = new URL(window.location.href);
      url.search = withoutStripe(url.search);
      window.history.replaceState(window.history.state, "", url);
    }
    let cancelled = false;
    (async () => {
      const outcome = await call(() => paymentsAccount());
      if (cancelled) return;
      if (outcome.status === 200 && outcome.data) {
        setAccount(outcome.data);
        if (back === "refresh" && owner && paymentsAction(outcome.data.state, outcome.data.country_supported, outcome.data.currency_supported) === "continue") void go();
      } else setFailed(true);
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once the plan read settles, so a refresh can reconnect
  }, [planLoading]);

  async function dashboard() {
    setFailed(false);
    setSignedOut(false);
    const result = await openStripeDashboard(call, busyRef, setBusy);
    if (result === "signedOut") setSignedOut(true);
    else if (result === "failed") setFailed(true);
  }

  async function go() {
    if (busyRef.current || !available) return;
    busyRef.current = true;
    setBusy(true);
    setFailed(false);
    setSignedOut(false);
    setCurrencyChanged(false);
    const outcome = await call(() => paymentsConnect({ body: { locale } }), { write: true });
    // Reset even before leaving for Stripe: Back may restore this page from the bfcache as it was.
    busyRef.current = false;
    setBusy(false);
    if (outcome.status === 200 && outcome.data) {
      window.location.assign(outcome.data.url);
      return;
    }
    const why = connectError(outcome);
    if (why === "signedOut") setSignedOut(true);
    else if (why === "failed") setFailed(true);
    else {
      // ZIF-102: the currency, not Stripe. Read the state again: the panel then says why.
      if (why === "currencyChanged") setCurrencyChanged(true);
      const again = await call(() => paymentsAccount());
      if (again.status === 200 && again.data) setAccount(again.data);
    }
  }

  const action = account && owner ? paymentsAction(account.state, account.country_supported, account.currency_supported) : null;
  let body: string | null = null;
  if (account) {
    if (account.state === "off") body = t("paymentsOff");
    else if (!account.country_supported) body = t("paymentsCountry");
    else if (account.state === "none" && !account.currency_supported) body = t("paymentsCurrency", { currency: currencyName(account.currency, locale) });
    else if (!planLoading) {
      const statusKey = {
        none: available ? "paymentsNone" : "paymentsAccountNone",
        onboarding: available ? "paymentsOnboarding" : "paymentsAccountOnboarding",
        ready: available ? "paymentsReady" : "paymentsAccountReady",
      } as const;
      const status = t(statusKey[account.state]);
      body = available || entitlements === null ? status : t("paymentsPlanDisabled", { status });
    }
  }

  return (
    <fieldset className={`${styles.section} ${styles.sectionRow} ${styles.settingsSection}`} disabled={disabled}>
      <legend>
        <h2>{t("paymentsHeading")}</h2>
      </legend>
      <p className={styles.sectionDesc}>{t("paymentsDesc")}</p>
      <div className={styles.sectionBody}>
      <FeatureNote feature="online_payments" />
      {/* The section is a tab of its own (ZIF-161): say it is loading rather than show an empty tab. */}
      {!account && !failed && (
        <p className={uiStyles.hint} role="status">
          {t("paymentsLoading")}
        </p>
      )}
      {signedOut && <SignedOutBanner />}
      {body && <p className={styles.body}>{body}</p>}
      {/* ZIF-80: the plan's fees, and where Stripe's own are; no Stripe rate is copied here. */}
      {account && account.state !== "off" && account.country_supported && (
        <>
          <p className={styles.body}>
            {t("paymentsFees", { direct: percentOf(account.platform_fee_direct, locale), marketplace: percentOf(account.platform_fee_marketplace, locale) })}
          </p>
          <p className={styles.body}>
            <span>
              {t("paymentsStripeFee")}{" "}
              <a className={uiStyles.inlineLink} href={stripePricingUrl(account.country)} target="_blank" rel="noopener noreferrer">
                {t("paymentsStripePricing")}
                <span className={uiStyles.srOnly}> {t("opensInNewTab")}</span>
              </a>
            </span>
          </p>
        </>
      )}
      {(action === "connect" || action === "continue") && (
        <button className={`${uiStyles.button} ${uiStyles.primary}`} type="button" disabled={!available} aria-disabled={busy || undefined} onClick={() => go()}>
          {busy && <span className={uiStyles.spinner} aria-hidden="true" />}
          {t(action === "connect" ? "paymentsConnect" : "paymentsContinue")}
        </button>
      )}
      {account && owner && account.state !== "off" && account.country_supported && account.min_amount_minor !== null && switchRow(formatMoney(account.min_amount_minor, account.currency, locale), account.state === "ready")}
      {action === "dashboard" && (
        <p className={styles.row}>
          <button className={uiStyles.textButton} type="button" aria-disabled={busy || undefined} onClick={dashboard}>
            {t("paymentsDashboard")}
            <svg aria-hidden="true" viewBox="0 0 16 16" width="14" height="14">
              <path d="M4 12l8-8M7 4h5v5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            <span className={uiStyles.srOnly}>{t("opensInNewTab")}</span>
          </button>
        </p>
      )}
      {currencyChanged && <Banner tone="note">{t("paymentsCurrencyChanged")}</Banner>}
      {failed && <Banner tone="error">{t("paymentsFailed")}</Banner>}
      </div>
    </fieldset>
  );
}
