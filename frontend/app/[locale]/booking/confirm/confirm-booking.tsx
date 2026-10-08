"use client";

import { useLocale, useTranslations } from "next-intl";
import { useEffect, useRef, useState, useSyncExternalStore } from "react";

import { bookingHoldsConfirm, bookingHoldsRead, type HoldView } from "@/api-client";
import { takeToken } from "@/lib/account";
import { firstStage, localWhen } from "@/lib/booking-link";
import { confirmScreen, holdEnded } from "@/lib/booking-page";
import { dateLocale } from "@/lib/console";
import { linkStore, type OpenedLink } from "@/lib/link";
import { type Locale } from "@/lib/services";
import { zoneCity } from "@/lib/week";

import { Checkout, type Done, DoneScreen, Held } from "../../[slug]/booking-page";
import { FieldError, NoScript, Outcome, send } from "../../_ui/parts";
import styles from "../../_ui/ui.module.css";

const store = linkStore(() => takeToken(window), globalThis);

type View =
  | { is: "opening" }
  | { is: "dead" }
  | { is: "failed" }
  | { is: "form"; hold: HoldView }
  | { is: "taken"; hold: HoldView }
  | { is: "done"; hold: HoldView; done: Done };

export function ConfirmBooking() {
  const link = useSyncExternalStore(store.subscribe, store.snapshot, () => undefined);
  const t = useTranslations("BookingPage");
  const form = useTranslations("Form");
  if (link === undefined) {
    // Server-rendered and before the page runs: without JavaScript the link can't be used.
    return (
      <Page>
        <h1 className={styles.heading}>{t("confirmTitle")}</h1>
        <NoScript>{form("noScript")}</NoScript>
      </Page>
    );
  }
  // Keyed: every link opened in this tab starts over.
  return <Confirm key={link?.opened ?? -1} link={link} />;
}

function Page({ children }: { children: React.ReactNode }) {
  const t = useTranslations("BookingPage");
  return (
    <>
      <main className={styles.screen}>
        <div className={`${styles.col} ${styles.bookingWide}`}>{children}</div>
      </main>
      <footer className={styles.minimalFooter}>
        <span>
          {t("footerBy")} <b>ziftbook</b>
        </span>
      </footer>
    </>
  );
}

function Confirm({ link }: { link: OpenedLink | null }) {
  const t = useTranslations("BookingPage");
  const opening = useTranslations("BookingLink");
  const locale = useLocale();
  const dLocale = dateLocale(locale);
  const token = link?.token ?? null;
  const [view, setView] = useState<View>(() => (firstStage(token) === "exchange" ? { is: "opening" } : { is: "dead" }));
  const [name, setName] = useState("");
  const [phone, setPhone] = useState("");
  const [nameError, setNameError] = useState<string | null>(null);
  const [banner, setBanner] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const working = useRef(false);
  const started = useRef(false);
  const nameRef = useRef<HTMLInputElement>(null);

  /** Only reads: opening the link (or a mail scanner fetching this page) changes nothing. */
  async function read(note?: string) {
    setView({ is: "opening" });
    const answer = await send(bookingHoldsRead({ body: { token: token! } }));
    if (answer.status === 200 && answer.data) {
      setView({ is: "form", hold: answer.data });
      setBanner(note ?? null);
    } else setView(answer.status === 404 ? { is: "dead" } : { is: "failed" });
  }

  useEffect(() => {
    // Once, even when development mode runs effects twice: the read is rate limited.
    if (started.current || firstStage(token) !== "exchange") return;
    started.current = true;
    void read();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function book(hold: HoldView) {
    if (working.current) return;
    if (!name.trim()) {
      setNameError(t("errName"));
      nameRef.current?.focus();
      return;
    }
    working.current = true;
    setBusy(true);
    setBanner(null);
    const answer = await send(
      bookingHoldsConfirm({
        body: { token: token!, name, ...(phone.trim() ? { phone: phone.trim() } : {}), policy_version: hold.policy_version, consents: {} },
      }),
    );
    working.current = false;
    setBusy(false);
    const screen = confirmScreen(answer);
    if (screen === "done" && answer.data) {
      store.forget(link);
      setView({
        is: "done",
        hold,
        done: {
          status: answer.data.status,
          service: { name: hold.service_name, duration_minutes: answer.data.duration_minutes },
          worker: { display_name: answer.data.worker_display_name },
          starts_at: answer.data.starts_at,
          price: answer.data.price,
          email: hold.email,
        },
      });
    } else if (screen === "dead") setView({ is: "dead" });
    else if (screen === "taken") setView({ is: "taken", hold });
    else if (screen === "policyChanged") void read(t("policyChanged"));
    else setBanner(screen === "tooMany" ? t("tooMany") : screen === "fieldErrors" ? t("checkDetails") : t("unknownOutcome"));
  }

  if (view.is === "opening") {
    return (
      <Page>
        <p className={styles.hint} role="status">
          {opening("opening")}
        </p>
      </Page>
    );
  }
  if (view.is === "dead") {
    return (
      <Page>
        <div className={styles.outcomeGrid}>
          <Outcome icon="expired" title={t("deadTitle")} lede={t("deadLede")}>
            <p className={styles.hint}>{t("deadHint")}</p>
          </Outcome>
        </div>
      </Page>
    );
  }
  if (view.is === "failed") {
    return (
      <Page>
        <div className={styles.outcomeGrid}>
          <Outcome icon="calendar" title={t("loadErrorTitle")} lede={t("loadErrorLede")}>
            <button className={`${styles.button} ${styles.primary}`} type="button" onClick={() => void read()}>
              {t("tryAgain")}
            </button>
          </Outcome>
        </div>
      </Page>
    );
  }

  const hold = view.hold;
  const zone = hold.timezone;
  const header = (
    <div className={styles.bizHeader}>
      <h1>{hold.business}</h1>
      <p className={styles.bizMeta}>
        <span>{zoneCity(zone)}</span>
        <span aria-hidden="true">·</span>
        <span>{t("timesIn", { city: zoneCity(zone) })}</span>
      </p>
    </div>
  );

  if (view.is === "done") {
    return (
      <Page>
        {header}
        <DoneScreen page={{ name: hold.business, language: hold.language }} done={view.done} locale={locale as Locale} dLocale={dLocale} zone={zone} t={t} />
      </Page>
    );
  }
  if (view.is === "taken") {
    const w = localWhen(hold.starts_at, zone, dLocale);
    return (
      <Page>
        {header}
        <div className={styles.outcomeGrid}>
          <Outcome icon="calendar" title={t("takenTitle")} lede={t("takenLede", { when: `${w.date}, ${w.time}` })}>
            <a className={`${styles.button} ${styles.primary}`} href={`/${locale}/${hold.slug}?service=${hold.service_id}`}>
              {t("otherTime")}
            </a>
          </Outcome>
        </div>
      </Page>
    );
  }

  const until = localWhen(hold.held_until, zone, dLocale).time;
  const minutes = (Date.parse(hold.ends_at) - Date.parse(hold.starts_at)) / 60_000;
  return (
    <Page>
      {header}
      <div className={styles.bookingLayout}>
        <div className={styles.flowCol}>
          <h2 className={styles.pageTitle}>{t("confirmTitle")}</h2>
          <div className={styles.stack}>
            <Held>{holdEnded(hold.held_until, new Date()) ? t("confirmLate", { until }) : t("confirmHold", { until })}</Held>
            <div className={styles.readonly}>
              {/* No prefill, ever: a forwarded link must not show what the business holds. */}
              <span>{t("bookingAs", { email: "" }).trim()}</span>
              <b>{hold.email}</b>
            </div>
            <div className={styles.form}>
              <div className={styles.row2}>
                <div className={styles.field}>
                  <label className={styles.label} htmlFor="hc-name">
                    {t("name")}
                  </label>
                  <div className={styles.input}>
                    <input
                      ref={nameRef}
                      id="hc-name"
                      name="name"
                      autoComplete="name"
                      value={name}
                      onChange={(e) => {
                        setName(e.target.value);
                        setNameError(null);
                      }}
                      aria-invalid={nameError ? true : undefined}
                      aria-describedby={nameError ? "hc-name-err" : undefined}
                    />
                  </div>
                  {nameError && <FieldError id="hc-name-err">{nameError}</FieldError>}
                </div>
                <div className={styles.field}>
                  <label className={styles.label} htmlFor="hc-phone">
                    {t("phone")} <span className={styles.hint}>{t("optional")}</span>
                  </label>
                  <div className={styles.input}>
                    <input id="hc-phone" name="phone" type="tel" autoComplete="tel" value={phone} onChange={(e) => setPhone(e.target.value)} aria-describedby="hc-phone-hint" />
                  </div>
                  <p className={styles.hint} id="hc-phone-hint">
                    {t("phoneHint")}
                  </p>
                </div>
              </div>
            </div>
          </div>
        </div>
        <Checkout
          page={{ name: hold.business, cancellation: hold.cancellation, auto_confirm: hold.auto_confirm }}
          s2={{
            service: { name: hold.service_name, duration_minutes: minutes, price: hold.price, workers: [{ id: "held", display_name: hold.worker_display_name }] },
            slot: hold.starts_at,
            worker: hold.worker_display_name ? "held" : "any",
            pick: null,
          }}
          locale={locale}
          dLocale={dLocale}
          zone={zone}
          businessLanguage={hold.language as Locale}
          t={t}
          banner={banner ? { tone: "error", text: banner } : null}
          busy={busy}
          verifying={false}
          turnstileSiteKey={null}
          onTurnstileLoad={() => {}}
          onBook={() => void book(hold)}
          hidden={false}
        />
      </div>
    </Page>
  );
}
