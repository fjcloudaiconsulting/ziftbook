"use client";

import Script from "next/script";
import { useTranslations } from "next-intl";

import type { BookingPageOut } from "@/api-client";
import { Link } from "@/i18n/navigation";
import { addDays, localDay, localWhen } from "@/lib/booking-link";
import { cancellationState, freeNamed, nextWeekDisabled, ownPolicyText, slotsFor, withName } from "@/lib/booking-page";
import { formatMoney } from "@/lib/money";
import { type Locale } from "@/lib/services";
import { zoneCity } from "@/lib/week";

import { AlertIcon, Banner, Outcome, Submit } from "../_ui/parts";
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
        <fieldset className={styles.pillSet}>
          <legend className={styles.hint}>{t("withWho")}</legend>
          <div className={styles.pills}>
            <label className={styles.pill2}>
              <input type="radio" name="worker" checked={worker === "any"} onChange={() => onWorker("any")} />
              {worker === "any" && "✓ "}
              {t("anyone")}
            </label>
            {named.map((w) => (
              <label className={styles.pill2} key={w.id}>
                <input type="radio" name="worker" checked={worker === w.id} onChange={() => onWorker(w.id)} />
                {worker === w.id && "✓ "}
                {w.display_name}
              </label>
            ))}
          </div>
        </fieldset>
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

/** What the checkout aside needs to know about the choice: today's flow, or (ZIF-117) the held
 * booking on the confirm page. */
export type Chosen = {
  service: Pick<Service, "name" | "duration_minutes" | "price" | "workers">;
  slot: string | null;
  worker: string;
  pick: string | null;
};

export function Checkout({
  page,
  s2,
  locale,
  dLocale,
  zone,
  businessLanguage,
  t,
  banner,
  busy,
  verifying,
  turnstileSiteKey,
  onTurnstileLoad,
  onBook,
  hidden,
  bare = false,
  linkFirst = false,
}: {
  page: Pick<BookingPageOut, "name" | "cancellation" | "auto_confirm">;
  s2: Chosen | null;
  locale: string;
  dLocale: string;
  zone: string;
  businessLanguage: Locale;
  t: ReturnType<typeof useTranslations>;
  banner: { tone: "note" | "error"; text: string } | null;
  busy: boolean;
  verifying: boolean;
  turnstileSiteKey: string | null;
  /** True on the done screen: the aside (Turnstile included) stays mounted — never conditionally
   * removed, so the same live widget survives into the next "Book another" cycle — just hidden. */
  hidden: boolean;
  /** ZIF-117, on "Check your email": only the Turnstile box, so "Send the email again" can still
   * show a challenge (the same live widget, never remounted). */
  bare?: boolean;
  /** ZIF-117: the button emails a link, and the hold note replaces the fine print under it. */
  linkFirst?: boolean;
  onTurnstileLoad(): void;
  onBook(): void;
}) {
  const c = page.cancellation;
  const service = s2?.service ?? null;
  const slot = s2?.slot ?? null;
  // undefined: nothing chosen yet; null: a person with no name, so no "With" row at all.
  const workerName = s2 ? withName(s2.service.workers, s2.worker, s2.pick, t("anyone")) : undefined;
  const name = service ? localized(service.name, locale as Locale, businessLanguage) : null;
  const ownText = ownPolicyText(c.text, locale, businessLanguage);

  let whenText = <dd className={styles.unset}>{t("notChosen")}</dd>;
  let termLine: React.ReactNode;
  if (slot && service) {
    const start = new Date(slot);
    const end = new Date(start.getTime() + service.duration_minutes * 60_000);
    const w = localWhen(slot, zone, dLocale);
    const endTime = new Intl.DateTimeFormat(dLocale, { hour: "2-digit", minute: "2-digit", timeZone: zone }).format(end);
    whenText = <dd>{t("whenFormat", { day: w.date, from: w.time, to: endTime })}</dd>;
    const state = cancellationState(new Date(), start, c.free_cancellation_hours);
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

  const label = linkFirst ? (busy ? t("sending") : t("emailLink")) : busy ? (page.auto_confirm ? t("booking") : t("sending")) : page.auto_confirm ? t("book") : t("request");

  return (
    <aside className={bare ? undefined : styles.checkout} aria-labelledby={bare ? undefined : "checkout-h"} hidden={hidden}>
      {!bare && (
      <>
      <h2 id="checkout-h" className={styles.checkoutHead}>
        {t("yourBooking")}
      </h2>
      <dl className={styles.summary}>
        <div className={styles.summaryRow}>
          <dt>{t("service")}</dt>
          {service && name ? <dd lang={name.lang ?? undefined}>{name.text}</dd> : <dd className={styles.unset}>{t("notChosen")}</dd>}
        </div>
        <div className={styles.summaryRow}>
          <dt>{t("when")}</dt>
          {whenText}
        </div>
        {workerName !== null && (
          <div className={styles.summaryRow}>
            <dt>{t("withRow")}</dt>
            {workerName !== undefined ? <dd>{workerName}</dd> : <dd className={styles.unset}>{t("notChosen")}</dd>}
          </div>
        )}
        <div className={styles.summaryRow}>
          <dt>{t("price")}</dt>
          {service ? <dd>{formatMoney(service.price.amount_minor, service.price.currency, locale)}</dd> : <dd className={styles.unset}>{t("notChosen")}</dd>}
        </div>
      </dl>
      <section className={styles.terms} aria-labelledby="terms-h">
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
      {!page.auto_confirm && <Banner tone="note">{t("approvalNote", { biz: page.name })}</Banner>}
      {verifying && <Banner tone="note">{t("verifying")}</Banner>}
      {banner && <Banner tone={banner.tone}>{banner.text}</Banner>}
      </>
      )}
      {/* Rendered visibly (never display:none) and kept mounted for the whole page's life — an
       * interaction-only challenge must be able to actually appear here, and "Book another" must
       * reuse the same live widget rather than a dead one from a screen that unmounted it. */}
      {turnstileSiteKey && (
        <>
          <div id="turnstile-container" className={styles.turnstileBox} />
          {/* onReady, not onLoad: next/script's LoadCache means onLoad only fires the very first
           * time this script URL is ever loaded on the page — a client-side locale switch or a
           * soft nav to another slug remounts BookingPage without the browser reloading the
           * script, so onLoad would never fire again and no widget would ever get rendered.
           * onReady fires on every mount, script-cached or not. */}
          <Script src="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit" strategy="afterInteractive" onReady={onTurnstileLoad} />
        </>
      )}
      {!bare && (
        <>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              onBook();
            }}
          >
            <Submit busy={busy} busyLabel={label}>
              {label}
            </Submit>
          </form>
          <p className={styles.fine}>
            {linkFirst ? t("holdNote") : t("fine", { biz: page.name })}{" "}
            <Link href="/privacy" target="_blank">
              {t("finePrivacy")}
              <span className={styles.srOnly}> {t("newTab")}</span>
            </Link>
          </p>
        </>
      )}
    </aside>
  );
}

/** The booked / requested screen: today's page, and (ZIF-117) the confirm page after the click,
 * which has no "Book another appointment" (no flow to restart there). */
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
  onAgain?(): void;
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
        {onAgain && (
          <button className={`${styles.button} ${styles.secondary}`} type="button" onClick={onAgain}>
            {t("another")}
          </button>
        )}
      </Outcome>
    </div>
  );
}

/** The clock-and-note line (ZIF-117): how long a time is held. */
export function Held({ children }: { children: React.ReactNode }) {
  return (
    <div className={styles.held}>
      <svg aria-hidden="true" viewBox="0 0 16 16" width="16" height="16">
        <circle cx="8" cy="8" r="6.3" fill="none" stroke="currentColor" strokeWidth="1.5" />
        <path d="M8 4.8V8l2.2 1.5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      </svg>
      <span>{children}</span>
    </div>
  );
}
