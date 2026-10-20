"use client";

import { useLocale, useTranslations } from "next-intl";
import { type ReactNode, useEffect, useRef, useState } from "react";

import { type PaymentAccountOut, paymentsAccount, paymentsConnect } from "@/api-client";
import { useRouter } from "@/i18n/navigation";
import { formatMoney } from "@/lib/money";
import { paymentsAction } from "@/lib/settings";

import { SignedOutBanner, useConsole } from "../../_ui/console";
import styles from "../../_ui/console.module.css";
import { Banner } from "../../_ui/parts";
import { openStripeDashboard } from "../../_ui/stripe-dashboard";
import uiStyles from "../../_ui/ui.module.css";

/** Online payments: the Stripe connection's state, and the one action it allows (owners only). `switchRow`
 * is the settings form's own checkbox that turns the checkout's payment step on (it saves with the page). */
export function Payments({ disabled, switchRow }: { disabled: boolean; switchRow(min: string, ready: boolean): ReactNode }) {
  const { session, call } = useConsole();
  const t = useTranslations("Console.settings");
  const locale = useLocale() as "en" | "nl" | "pt";
  const router = useRouter();
  const [account, setAccount] = useState<PaymentAccountOut | null>(null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const [signedOut, setSignedOut] = useState(false);
  const busyRef = useRef(false);
  const owner = session.role === "owner";

  // Stripe sends the owner back with ?stripe=return (read the state again) or ?stripe=refresh (the
  // link expired: fetch a fresh one); either way the param goes once handled.
  useEffect(() => {
    const back = new URLSearchParams(window.location.search).get("stripe");
    if (back) router.replace("/settings");
    let cancelled = false;
    (async () => {
      const outcome = await call(() => paymentsAccount());
      if (cancelled) return;
      if (outcome.status === 200 && outcome.data) {
        setAccount(outcome.data);
        if (back === "refresh" && owner && paymentsAction(outcome.data.state, outcome.data.country_supported) === "continue") void go("connect");
      } else setFailed(true);
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once on load
  }, []);

  async function dashboard() {
    setFailed(false);
    setSignedOut(false);
    const result = await openStripeDashboard(call, busyRef, setBusy);
    if (result === "signedOut") setSignedOut(true);
    else if (result === "failed") setFailed(true);
  }

  async function go(kind: "connect") {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setFailed(false);
    setSignedOut(false);
    const outcome = await call(() => paymentsConnect({ body: { locale } }), { write: true });
    // Reset even before leaving for Stripe: Back may restore this page from the bfcache as it was.
    busyRef.current = false;
    setBusy(false);
    if (outcome.status === 200 && outcome.data) {
      window.location.assign(outcome.data.url);
      return;
    }
    if (outcome.status === 401) setSignedOut(true);
    else setFailed(true);
  }

  if (!account && !failed) return null;
  const action = account && owner ? paymentsAction(account.state, account.country_supported) : null;
  const body = !account
    ? null
    : account.state === "off"
      ? t("paymentsOff")
      : !account.country_supported
        ? t("paymentsCountry")
        : t(account.state === "none" ? "paymentsNone" : account.state === "onboarding" ? "paymentsOnboarding" : "paymentsReady");

  return (
    <fieldset className={`${styles.section} ${styles.settingsSection}`} disabled={disabled}>
      <legend>
        <h2>{t("paymentsHeading")}</h2>
      </legend>
      {signedOut && <SignedOutBanner />}
      {body && <p className={styles.body}>{body}</p>}
      {account && owner && account.state !== "off" && account.country_supported && account.min_amount_minor !== null && switchRow(formatMoney(account.min_amount_minor, account.currency, locale), account.state === "ready")}
      {(action === "connect" || action === "continue") && (
        <button className={`${uiStyles.button} ${uiStyles.primary}`} type="button" aria-disabled={busy || undefined} onClick={() => go("connect")}>
          {busy && <span className={uiStyles.spinner} aria-hidden="true" />}
          {t(action === "connect" ? "paymentsConnect" : "paymentsContinue")}
        </button>
      )}
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
      {failed && <Banner tone="error">{t("paymentsFailed")}</Banner>}
    </fieldset>
  );
}
