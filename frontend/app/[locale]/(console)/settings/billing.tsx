"use client";

import { useLocale, useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { billingCheckout, billingPortal, type PlanOption, type UrlOut } from "@/api-client";
import { billingError, canManage, canSubscribeTo, feeLabel, isStripeUrl, priceLabel, priceNote, stillBilled } from "@/lib/billing";
import { dateLocale } from "@/lib/console";

import { JSON_WRITE, SignedOutBanner, useConsole } from "../../_ui/console";
import styles from "../../_ui/console.module.css";
import { Banner } from "../../_ui/parts";
import uiStyles from "../../_ui/ui.module.css";

/** The plan tab (ZIF-79): the current plan and where it comes from, the plans on offer with their price and
 * both platform fees, and Stripe's Checkout (subscribe) or customer portal (manage). Everyone sees the plan;
 * only the owner gets the buttons (the API sends the offers to owners only). */
export function Billing() {
  const { session, settings, entitlements: e, reloadEntitlements, call } = useConsole();
  const t = useTranslations("Console.billing");
  const locale = useLocale();
  const owner = session.role === "owner";
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<ReturnType<typeof billingError> | null>(null);
  const [signedOut, setSignedOut] = useState(false);
  // 409 already_subscribed: the plan read was stale. Offer the portal until the read catches up.
  const [subscribed, setSubscribed] = useState(false);
  const [tried, setTried] = useState(false);
  const busyRef = useRef(false);

  // Checkout sends the owner back with ?billing=done: read the plan again, and drop the param once handled
  // (Settings owns ?tab). Read at mount, before the effect drops it.
  const [back] = useState(() => new URLSearchParams(window.location.search).get("billing"));
  useEffect(() => {
    if (back) {
      const url = new URL(window.location.href);
      url.searchParams.delete("billing");
      window.history.replaceState(window.history.state, "", url);
    }
    // The Shell's own read may have failed or still be in flight: this one settles "loading" either way.
    if (back || !e) void reloadEntitlements().then(() => setTried(true));
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once on load
  }, []);

  async function open(id: string, request: Parameters<typeof call<UrlOut>>[0]) {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(id);
    setError(null);
    setSignedOut(false);
    const outcome = await call(request, { write: true });
    // Reset even before leaving for Stripe: Back may restore this page from the bfcache as it was.
    busyRef.current = false;
    setBusy(null);
    if (outcome.status === 200 && outcome.data) {
      if (isStripeUrl(outcome.data.url)) {
        window.location.assign(outcome.data.url);
        return;
      }
      return setError("failed");
    }
    if (outcome.status === 401) return setSignedOut(true);
    const why = billingError(outcome);
    if (why === "alreadySubscribed") {
      setSubscribed(true);
      void reloadEntitlements();
    }
    // The plan list then shows the offer's discount as gone.
    if (why === "couponUnavailable") void reloadEntitlements();
    setError(why);
  }

  const day = (iso: string) => new Intl.DateTimeFormat(dateLocale(locale), { dateStyle: "long", timeZone: settings.timezone }).format(new Date(iso));
  const name = (names: Record<string, string>) => names[locale] ?? names.en ?? Object.values(names)[0] ?? "";

  function source() {
    if (!e) return null;
    if (e.source === "grant") return e.grant_ends_at ? t("sourceGrantUntil", { date: day(e.grant_ends_at) }) : t("sourceGrant");
    if (e.source === "trial" && e.trial_ends_at) return t("sourceTrial", { date: day(e.trial_ends_at) });
    if (e.source === "subscription" && e.period_end) return t(e.cancel_at_period_end ? "sourceEnds" : "sourceSubscription", { date: day(e.period_end) });
    return t("sourceDefault");
  }

  const managed = !!e && canManage(e, subscribed);
  const canSubscribe = owner && !!e && !stillBilled(e, subscribed) && e.source !== "grant";
  const places = e?.values.places;

  function renderOption(option: PlanOption) {
    const price = priceLabel(option, locale);
    const note = priceNote(option);
    const label = name(option.name);
    return (
      <li key={option.id}>
        <div className={styles.rowStatic}>
          <span className={styles.rowMain}>
            <span className={styles.rowTitle}>
              {label}
              {e && option.id === e.plan_id && <span className={styles.pill}>{t("currentBadge")}</span>}
            </span>
            <span className={styles.rowMeta}>{price ? t(price.key, price.values) : t("noPrice")}</span>
            {note && <span className={styles.rowMeta}>{t(note)}</span>}
            <span className={styles.rowMeta}>{t("feeDirect", { fee: feeLabel(option.platform_fee_direct, locale) })}</span>
            <span className={styles.rowMeta}>{t("feeMarketplace", { fee: feeLabel(option.platform_fee_marketplace, locale) })}</span>
            <span className={styles.rowMeta}>{t("stripeFee")}</span>
          </span>
          {canSubscribe && e && canSubscribeTo(e, option.id) && (
            <span className={styles.rowActions}>
              <button
                className={`${uiStyles.button} ${uiStyles.primary} ${uiStyles.small}`}
                type="button"
                aria-disabled={busy !== null || undefined}
                aria-label={t("subscribeTo", { plan: label })}
                onClick={() => open(option.id, () => billingCheckout({ body: { plan_id: option.id } }))}
              >
                {busy === option.id && <span className={uiStyles.spinner} aria-hidden="true" />}
                {busy === option.id ? t("busy") : t("subscribe")}
              </button>
            </span>
          )}
        </div>
      </li>
    );
  }

  return (
    <fieldset className={`${styles.section} ${styles.sectionRow} ${styles.settingsSection}`}>
      <legend>
        <h2>{t("heading")}</h2>
      </legend>
      <p className={styles.sectionDesc}>{t("desc")}</p>
      <div className={styles.sectionBody}>
        {signedOut && <SignedOutBanner />}
        {back === "done" && <Banner tone="info">{t("done")}</Banner>}
        {error && <Banner tone="error">{t(error === "failed" ? "failedGeneric" : error)}</Banner>}
        {!e && !tried && (
          <p className={uiStyles.hint} role="status">
            {t("loading")}
          </p>
        )}
        {!e && tried && (
          <>
            <Banner tone="error">{t("failed")}</Banner>
            <button className={uiStyles.textButton} type="button" onClick={() => void reloadEntitlements()}>
              {t("retry")}
            </button>
          </>
        )}
        {e && (
          <>
            <p className={styles.body}>
              <strong>{t("current", { plan: name(e.plan_name) })}</strong>
              <br />
              {source()}{" "}
              {typeof places === "number" ? t("placesOf", { used: e.members_used, limit: places }) : t("places", { used: e.members_used })}
              <br />
              {t("feeDirect", { fee: feeLabel(Number(e.values.platform_fee_direct), locale) })}
              <br />
              {t("feeMarketplace", { fee: feeLabel(Number(e.values.platform_fee_marketplace), locale) })}
            </p>
            {e.over_limit_since && <Banner tone="note">{t("overLimit")}</Banner>}
            {!owner && <p className={uiStyles.hint}>{t("ownerOnly")}</p>}
            {owner && e.source === "grant" && <p className={uiStyles.hint}>{t("granted")}</p>}
            {owner && managed && (
              <>
                <p>
                  <button
                    className={`${uiStyles.button} ${uiStyles.primary}`}
                    type="button"
                    aria-disabled={busy !== null || undefined}
                    onClick={() => open("portal", () => billingPortal(JSON_WRITE))}
                  >
                    {busy === "portal" && <span className={uiStyles.spinner} aria-hidden="true" />}
                    {busy === "portal" ? t("busy") : t("manage")}
                  </button>
                </p>
                <p className={uiStyles.hint}>{t("manageHint")}</p>
              </>
            )}
            {e.offers.length > 0 && (
              <>
                <h3 className={styles.heading2}>{t("optionsHeading")}</h3>
                <ul className={styles.list}>
                  {e.offers.map(renderOption)}
                </ul>
              </>
            )}
            {back === "done" && (
              <button className={uiStyles.textButton} type="button" onClick={() => void reloadEntitlements()}>
                {t("checkAgain")}
              </button>
            )}
          </>
        )}
      </div>
    </fieldset>
  );
}
