"use client";

import { useTranslations } from "next-intl";

import type { BookingPageOut } from "@/api-client";
import { addDays, localDay, localWhen } from "@/lib/booking-link";
import { cancellationState, freeNamed, nextWeekDisabled, ownPolicyText, slotsFor } from "@/lib/booking-page";
import { formatMoney } from "@/lib/money";
import { type Locale } from "@/lib/services";
import { zoneCity } from "@/lib/week";

import { AlertIcon, Banner, Chevron, Outcome } from "../_ui/parts";
import { CheckIcon, Picker as SharedPicker } from "../_ui/picker";
import styles from "../_ui/ui.module.css";
import { MoveIcon } from "./icons";
import { type CacheEntry, localized, type Service, type Step2Plus, type Worker } from "./shared";

export type Done = {
  status: string;
  service: Pick<Service, "name" | "duration_minutes">;
  worker: Pick<Worker, "display_name"> | null;
  starts_at: string;
  price: { amount_minor: number; currency: string };
  email: string;
};

/** Step 3's "who" choice (ZIF-100): only when the time was picked under "Anyone available" on a
 * service with more than one person. Two or more named people free: native radios, "Anyone"
 * preselected; exactly one: a plain line. The 409 note (decision 5) sits above it. */
export function WhoAt({
  s2,
  banner,
  zone,
  dLocale,
  t,
  onPick,
}: {
  s2: Step2Plus;
  banner: { tone: "note" | "error"; text: string } | null;
  zone: string;
  dLocale: string;
  t: ReturnType<typeof useTranslations>;
  onPick(id: string | null): void;
}) {
  const named = s2.slot && s2.worker === "any" && s2.service.workers.length > 1 ? freeNamed(s2.service.workers, s2.free) : [];
  const time = s2.slot ? localWhen(s2.slot, zone, dLocale).time : "";
  return (
    <>
      {banner && <Banner tone={banner.tone}>{banner.text}</Banner>}
      {named.length > 1 && (
        <fieldset className={styles.pillSet} aria-describedby="bp-who-hint">
          <legend className={styles.label}>{t("whoAt", { time })}</legend>
          <p className={styles.hint} id="bp-who-hint">
            {t("whoHint")}
          </p>
          <div className={styles.pills}>
            <label className={styles.pill2}>
              <input type="radio" name="pick" checked={s2.pick === null} onChange={() => onPick(null)} />
              {s2.pick === null && "✓ "}
              {t("anyone")}
            </label>
            {named.map((w) => (
              <label className={styles.pill2} key={w.id}>
                <input type="radio" name="pick" checked={s2.pick === w.id} onChange={() => onPick(w.id)} />
                {s2.pick === w.id && "✓ "}
                {w.display_name}
              </label>
            ))}
          </div>
        </fieldset>
      )}
      {/* Anyone else free then (unnamed, or not on this page's roster yet) may still be assigned. */}
      {named.length === 1 && <p className={styles.lone}>{t(s2.free.length > 1 ? "onlyChoosable" : "onlyFree", { name: named[0].display_name!, time })}</p>}
    </>
  );
}

export function Picker({
  page,
  s2,
  cache,
  firstFree,
  banner,
  dLocale,
  zone,
  t,
  onWorker,
  onWeek,
  onDay,
  onSlot,
  onJump,
  onAnyone,
  onRetry,
}: {
  page: BookingPageOut;
  s2: Step2Plus;
  cache: Map<string, CacheEntry>;
  firstFree: string | null;
  banner: { tone: "note" | "error"; text: string } | null;
  dLocale: string;
  zone: string;
  t: ReturnType<typeof useTranslations>;
  onWorker(id: string): void;
  onWeek(delta: number): void;
  onDay(day: string): void;
  onSlot(slot: string, free: string[]): void;
  onJump(day: string): void;
  onAnyone(): void;
  onRetry(): void;
}) {
  const { service, worker } = s2;
  // Unnamed people are never offered (decision 6 option A); their times stay under "Anyone".
  const named = service.workers.filter((w) => w.display_name);
  const today = localDay(new Date(), zone);

  let from = s2.weekStart;
  while (addDays(from, 13) < s2.weekStart) from = addDays(from, 14);
  while (s2.weekStart < from) from = addDays(from, -14);
  const entry = cache.get(`${service.id}|${from}`);
  const win = typeof entry === "object" ? entry : null;

  const nextDisabled = nextWeekDisabled(today, addDays(s2.weekStart, 7), page.booking_horizon_days);
  const firstLabel = firstFree
    ? new Intl.DateTimeFormat(dLocale, { weekday: "long", day: "numeric", month: "long", timeZone: "UTC" }).format(new Date(`${firstFree}T12:00:00Z`))
    : null;

  return (
    <>
      {banner && <Banner tone={banner.tone}>{banner.text}</Banner>}
      {service.workers.length > 1 && named.length > 0 && (
        <div className={styles.field}>
          <label className={styles.label} htmlFor="bp-worker">
            {t("withWho")}
          </label>
          <div className={styles.input}>
            <select id="bp-worker" value={worker} onChange={(e) => onWorker(e.target.value)}>
              <option value="any">{t("anyone")}</option>
              {named.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.display_name}
                </option>
              ))}
            </select>
            <Chevron select />
          </div>
        </div>
      )}
      {service.workers.length === 1 && service.workers[0].display_name && <p className={styles.hint}>{t("with", { name: service.workers[0].display_name })}</p>}

      <SharedPicker
        weekStart={s2.weekStart}
        zone={zone}
        slots={win && slotsFor(win, worker)}
        day={s2.day}
        slot={s2.slot}
        taken={s2.taken}
        nextDisabled={nextDisabled}
        failed={
          entry === "error" ? (
            <>
              <Banner tone="error">{t("pickerError")}</Banner>
              <button className={styles.textButton} type="button" onClick={onRetry}>
                {t("tryAgain")}
              </button>
            </>
          ) : undefined
        }
        empty={
          <>
            {firstLabel && <span>{t("noWeekBody", { first: firstLabel })}</span>}
            <div>
              {firstFree && firstLabel && (
                <button className={`${styles.button} ${styles.primary} ${styles.small}`} type="button" onClick={() => onJump(firstFree)}>
                  {t("seeNext", { first: firstLabel })}
                </button>
              )}
              {worker !== "any" && (
                <button className={`${styles.button} ${styles.secondary} ${styles.small}`} type="button" onClick={onAnyone}>
                  {t("tryAnyone")}
                </button>
              )}
            </div>
          </>
        }
        onWeek={onWeek}
        onDay={onDay}
        onSlot={(slot) => onSlot(slot, worker === "any" ? (win!.who[win!.slots.indexOf(slot)] ?? []) : [])}
      />

      {nextDisabled && <p className={styles.hint}>{t("horizon", { n: page.booking_horizon_days })}</p>}
    </>
  );
}

/** The cancellation terms box: free until a time once one is picked, else the plain hours; the
 * business's own words when it wrote some in the page language. Used by the summary aside. */
export function Terms({
  page,
  slot,
  locale,
  dLocale,
  zone,
  businessLanguage,
  t,
}: {
  page: Pick<BookingPageOut, "name" | "cancellation">;
  slot: string | null;
  locale: string;
  dLocale: string;
  zone: string;
  businessLanguage: Locale;
  t: ReturnType<typeof useTranslations>;
}) {
  const c = page.cancellation;
  const ownText = ownPolicyText(c.text, locale, businessLanguage);
  let termLine: React.ReactNode;
  if (slot) {
    const state = cancellationState(new Date(), new Date(slot), c.free_cancellation_hours);
    if (state.kind === "before") {
      const freeW = localWhen(state.freeUntil.toISOString(), zone, dLocale);
      termLine = (
        <li className={styles.termsFree}>
          <CheckIcon />
          <span>
            {t("freeUntil", { when: `${freeW.date}, ${freeW.time}` })} {t("afterFree")}
          </span>
        </li>
      );
    } else {
      termLine = (
        <li>
          <AlertIcon />
          <span>{t("lateWindow", { n: c.free_cancellation_hours })}</span>
        </li>
      );
    }
  } else {
    termLine = (
      <li className={styles.termsFree}>
        <CheckIcon />
        <span>
          {t("freeHours", { n: c.free_cancellation_hours })} {t("afterFree")}
        </span>
      </li>
    );
  }
  return (
    <section className={styles.terms} id="cancel-terms" aria-labelledby="terms-h">
      <h3 id="terms-h">{t("termsTitle")}</h3>
      <ul className={styles.termsList}>
        {termLine}
        <li>
          <MoveIcon />
          <span>{c.max_reschedules > 0 ? t("moveTerms", { n: c.reschedule_cutoff_hours, m: c.max_reschedules }) : t("moveOff")}</span>
        </li>
      </ul>
      {ownText && (
        <blockquote className={styles.quote} lang={businessLanguage}>
          <p>{ownText}</p>
          <footer lang={locale}>{t("ownWords", { biz: page.name })}</footer>
        </blockquote>
      )}
    </section>
  );
}

/** The booked / requested screen. */
export function DoneScreen({
  page,
  done,
  locale,
  dLocale,
  zone,
  t,
  onAgain,
}: {
  page: Pick<BookingPageOut, "name" | "language">;
  done: Done;
  locale: Locale;
  dLocale: string;
  zone: string;
  t: ReturnType<typeof useTranslations>;
  onAgain(): void;
}) {
  const pending = done.status === "pending";
  const start = new Date(done.starts_at);
  const end = new Date(start.getTime() + done.service.duration_minutes * 60_000);
  const w = localWhen(done.starts_at, zone, dLocale);
  const endTime = new Intl.DateTimeFormat(dLocale, { hour: "2-digit", minute: "2-digit", timeZone: zone }).format(end);
  const name = localized(done.service.name, locale, page.language as Locale);
  const lede = pending ? t("pendLede", { biz: page.name, email: done.email }) : t("okLede", { email: done.email });
  return (
    <div className={styles.outcomeGrid}>
      <Outcome icon={pending ? "mail" : "done"} title={pending ? t("pendTitle") : t("okTitle")} lede={lede}>
        <ul className={styles.doneList}>
          <li lang={name.lang ?? undefined}>{name.text}</li>
          <li>
            {t("whenFormat", { day: w.date, from: w.time, to: endTime })} {"("}
            {zoneCity(zone)}
            {")"}
          </li>
          <li>
            {done.worker?.display_name && `${done.worker.display_name} · `}
            {formatMoney(done.price.amount_minor, done.price.currency, locale)}
          </li>
        </ul>
        <p className={styles.hint}>{t("spam")}</p>
        <button className={`${styles.button} ${styles.secondary}`} type="button" onClick={onAgain}>
          {t("another")}
        </button>
      </Outcome>
    </div>
  );
}
