"use client";

import { useLocale, useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState } from "react";

import { type CurrencyState, settingsChangeCurrency, settingsReadCurrency } from "@/api-client";
import { askView, changeBody, currencyChangeError, currencyChoices, currencyLabel, currencyName } from "@/lib/currency";

import { SignedOutBanner, useConsole } from "../../_ui/console";
import styles from "../../_ui/console.module.css";
import { Banner, Chevron, problem } from "../../_ui/parts";
import uiStyles from "../../_ui/ui.module.css";

type Code = CurrencyState["options"][number]["code"];

function LockIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 16 16" width="15" height="15">
      <rect x="3.25" y="7" width="9.5" height="7" rx="1.5" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <path d="M5.5 7V5.2a2.5 2.5 0 0 1 5 0V7" fill="none" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  );
}

/**
 * ZIF-102: the business's currency, read-only with a Change that opens an ask (design 4b, option A).
 * The owner's ruling: it changes only until the business's first booking and before it connects
 * Stripe, so Change shows only while GET /api/settings/currency says it is open, and the hint says
 * why otherwise. Saved prices keep their numbers once the owner confirms how many there are.
 */
export function CurrencyField({ disabled }: { disabled: boolean }) {
  const { session, call, updateSession } = useConsole();
  const t = useTranslations("Console.settings");
  const errorsT = useTranslations("Console.errors");
  const form = useTranslations("Form");
  const locale = useLocale();
  const id = useId();
  const [state, setState] = useState<CurrencyState | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [asking, setAsking] = useState(false);
  const [next, setNext] = useState<Code | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "error" | "info" | "note"; text: string } | null>(null);
  const [signedOut, setSignedOut] = useState(false);
  const submitting = useRef(false);
  const changeButton = useRef<HTMLButtonElement>(null);
  const nextSelect = useRef<HTMLSelectElement>(null);
  const owner = session.role === "owner";

  async function load(): Promise<void> {
    const outcome = await call(() => settingsReadCurrency());
    if (outcome.status === 200 && outcome.data) {
      setState(outcome.data);
      setLoadFailed(false);
    } else setLoadFailed(true);
  }

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const outcome = await call(() => settingsReadCurrency());
      if (cancelled) return;
      if (outcome.status === 200 && outcome.data) setState(outcome.data);
      else setLoadFailed(true);
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once on load
  }, []);

  useEffect(() => {
    if (asking) nextSelect.current?.focus();
  }, [asking]);

  function open() {
    if (!state) return;
    setNext(null); // nothing chosen until the owner picks one
    setMessage(null);
    setSignedOut(false);
    setAsking(true);
  }

  function close() {
    setAsking(false);
    // Back to the button that opened the ask, once it is on screen again.
    setTimeout(() => changeButton.current?.focus(), 0);
  }

  async function change() {
    if (!state || !next || submitting.current) return;
    submitting.current = true;
    setBusy(true);
    setMessage(null);
    setSignedOut(false);
    try {
      const outcome = await call(() => settingsChangeCurrency({ body: changeBody(next, state.prices) }), { write: true });
      if (outcome.status === 200 && outcome.data) {
        setState(outcome.data);
        // The price forms read the session's currency (services.tsx): a new price is in this one.
        updateSession({ currency: outcome.data.currency });
        setAsking(false);
        setMessage({ tone: "info", text: t("currencyChanged", { currency: currencyName(outcome.data.currency, locale) }) });
        return;
      }
      const known = currencyChangeError(outcome);
      if (known === "recount") {
        // A price was saved or removed meanwhile: show the new count, and ask again.
        setState({ ...state, prices: outcome.total ?? state.prices });
        setMessage({ tone: "note", text: t("currencyRecount") });
      } else if (known === "locked") {
        // A booking or a Stripe account arrived meanwhile: the hint now says which.
        setAsking(false);
        await load();
        setMessage({ tone: "error", text: t("currencyNowLocked") });
      } else if (known === "signedOut") setSignedOut(true);
      else if (known === "ownerOnly") setMessage({ tone: "error", text: errorsT("ownerOnly") });
      else setMessage({ tone: "error", text: form(problem(outcome)) });
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }

  const current = state?.currency ?? session.currency;
  const changeable = Boolean(owner && state && state.change === "open" && currencyChoices(state).length > 0);
  const locks = state?.locked_by ?? [];
  const name = currencyName(current, locale);
  const hint = loadFailed
    ? t("currencyLoadFailed")
    : locks.length > 0
      ? locks.map((lock) => t(lock === "bookings" ? "currencyLockedBookings" : "currencyLockedStripe", { currency: name })).join(" ")
      : t("currencyHint");
  const ask = state ? askView(state, next, locale) : null;

  return (
    <>
      <div className={uiStyles.field} role="group" aria-labelledby={`${id}-label`}>
        <span className={uiStyles.label} id={`${id}-label`}>
          {t("currencyLabel")}
        </span>
        <div className={styles.url}>
          {locks.length > 0 && (
            <i className={styles.urlLock}>
              <LockIcon />
            </i>
          )}
          <span>{currencyLabel(current, locale)}</span>
          {changeable && !asking && (
            <button ref={changeButton} className={styles.copyButton} type="button" disabled={disabled} onClick={open} aria-describedby={`${id}-hint`}>
              {t("currencyChange")}
            </button>
          )}
        </div>
        <p className={uiStyles.hint} id={`${id}-hint`}>
          {hint}
        </p>
      </div>

      {asking && state && ask && (
        <div className={uiStyles.confirm} role="group" aria-labelledby={`${id}-ask`}>
          <strong id={`${id}-ask`}>{t("currencyAskTitle")}</strong>
          <div className={uiStyles.field}>
            <label className={uiStyles.label} htmlFor={`${id}-next`}>
              {t("currencyNew")}
            </label>
            <div className={uiStyles.input}>
              <select ref={nextSelect} id={`${id}-next`} value={next ?? ""} disabled={busy} onChange={(e) => setNext((e.target.value || null) as Code | null)}>
                <option value="" disabled>
                  {t("currencyPick")}
                </option>
                {currencyChoices(state).map((o) => (
                  <option key={o.code} value={o.code}>
                    {currencyLabel(o.code, locale)}
                  </option>
                ))}
              </select>
              <Chevron select />
            </div>
          </div>
          {ask.example && <p>{state.prices > 0 ? t("currencyAskBody", { count: state.prices, ...ask.example }) : t("currencyAskNoPrices")}</p>}
          {ask.warn && next && <p>{t("currencyStripeWarning", { currency: currencyName(next, locale) })}</p>}
          <p>{t("currencyAskOnce")}</p>
          {signedOut && <SignedOutBanner />}
          {message && <Banner tone={message.tone}>{message.text}</Banner>}
          <div className={uiStyles.actions}>
            <button
              className={`${uiStyles.button} ${uiStyles.primary}`}
              type="button"
              aria-disabled={busy || !ask.canConfirm || undefined}
              onClick={change}
            >
              {busy && <span className={uiStyles.spinner} aria-hidden="true" />}
              {next ? t("currencyYes", { currency: currencyName(next, locale) }) : t("currencyChange")}
            </button>
            <button
              className={`${uiStyles.button} ${uiStyles.secondary}`}
              type="button"
              aria-disabled={busy || undefined}
              onClick={() => {
                if (!busy) close();
              }}
            >
              {t("currencyKeep", { currency: name })}
            </button>
          </div>
        </div>
      )}
      {!asking && signedOut && <SignedOutBanner />}
      {!asking && message && <Banner tone={message.tone}>{message.text}</Banner>}
    </>
  );
}
