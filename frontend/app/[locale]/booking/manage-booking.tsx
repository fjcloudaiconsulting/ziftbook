"use client";

import { useLocale, useTranslations } from "next-intl";
import { useEffect, useRef, useState, useSyncExternalStore } from "react";

import {
  bookingLinkAvailability,
  bookingLinkCancel,
  bookingLinkConsents,
  bookingLinkRead,
  bookingLinkReschedule,
  bookingLinkSession,
  type LinkedBooking,
} from "@/api-client";
import { takeToken } from "@/lib/account";
import { addDays, answerScreen, copyMessage, firstStage, localDay, localWhen, refundMessage, slotsByDay, weekDays } from "@/lib/booking-link";
import { dateLocale } from "@/lib/console";
import { type OpenedLink, openedLink } from "@/lib/invite";
import { formatMoney } from "@/lib/money";
import { type Locale, serviceName } from "@/lib/services";
import { problemList, zoneCity } from "@/lib/week";

import { Banner, Heading, NoScript, type Outcome as Answer, Outcome, send, Submit } from "../_ui/parts";
import styles from "../_ui/ui.module.css";

let shown: OpenedLink | null = null;
let opened = 0;

// Only a new link in the address bar fires hashchange: taking the token out uses replaceState, which doesn't.
function onLinkOpened(changed: () => void) {
  const opening = () => {
    opened += 1;
    changed();
  };
  window.addEventListener("hashchange", opening);
  return () => window.removeEventListener("hashchange", opening);
}

/** The link that opened this page, or the one opened since; undefined on the server and while hydrating. */
function useBookingLink(): OpenedLink | null | undefined {
  return useSyncExternalStore(
    onLinkOpened,
    () => (shown = openedLink(shown, takeToken(window), opened)),
    () => undefined,
  );
}

type Note = "termsChanged" | "changed" | "notAllowed" | "consentDone";
type Pick = { is: "pick"; linked: LinkedBooking; first: string; slots?: Map<string, string[]>; day?: string; chosen?: string; taken?: boolean };
type View =
  | { is: "opening" }
  | { is: "notActive" }
  | { is: "another" }
  | { is: "booking"; linked: LinkedBooking; note?: Note }
  | { is: "cancel"; linked: LinkedBooking }
  | Pick
  | { is: "cancelled"; linked: LinkedBooking }
  | { is: "moved"; linked: LinkedBooking };

export function ManageBooking() {
  const link = useBookingLink();
  const t = useTranslations("BookingLink");
  const form = useTranslations("Form");

  if (link === undefined) {
    // Server-rendered and before the page runs: without JavaScript the link can't be used.
    return (
      <>
        <h1 className={styles.heading}>{t("title")}</h1>
        <NoScript>{form("noScript")}</NoScript>
      </>
    );
  }
  // Keyed: every link opened in this tab starts over.
  return <Manage key={link?.opened ?? -1} token={link?.token ?? null} />;
}

function Manage({ token }: { token: string | null }) {
  const t = useTranslations("BookingLink");
  const locale = useLocale();
  const [view, setView] = useState<View>(() => (firstStage(token) === "notActive" ? { is: "notActive" } : { is: "opening" }));
  const [failed, setFailed] = useState<(() => void) | null>(null);
  const [busy, setBusy] = useState(false);
  const working = useRef(false);
  const started = useRef(false);

  /** An answer no screen handles itself: every 404 is the same screen, link_changed its own, anything else a retry. */
  function otherwise(answer: Answer<unknown>, again: () => void) {
    const screen = answerScreen(answer);
    if (screen === "notActive") setView({ is: "notActive" });
    else if (screen === "anotherBooking") setView({ is: "another" });
    else setFailed(() => again);
  }

  async function open() {
    if (firstStage(token) === "exchange") {
      const session = await send(bookingLinkSession({ body: { token: token! } }));
      if (session.status !== 204) return otherwise(session, open);
    }
    await refresh();
  }

  async function refresh(note?: Note) {
    const read = await send(bookingLinkRead());
    if (read.status === 200 && read.data) setView({ is: "booking", linked: read.data, note });
    else otherwise(read, () => refresh(note));
  }

  useEffect(() => {
    // Once, even when development mode runs effects twice: the exchange is rate limited.
    if (started.current || firstStage(token) === "notActive") return;
    started.current = true;
    void open();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** One action at a time; a second press does nothing. */
  async function act(run: () => Promise<void>) {
    if (working.current) return;
    working.current = true;
    setBusy(true);
    setFailed(null);
    await run();
    working.current = false;
    setBusy(false);
  }

  function afterWrite(answer: Answer<LinkedBooking>, again: () => void, pick?: Pick) {
    const screen = answerScreen(answer);
    if (screen === "termsChanged" || screen === "changed" || screen === "notAllowed") return refresh(screen);
    if (screen === "refresh") return refresh();
    if (screen === "slotTaken" && pick) return loadWeek(pick.linked, pick.first, true);
    otherwise(answer, again);
  }

  async function loadWeek(linked: LinkedBooking, first: string, taken?: boolean) {
    setView({ is: "pick", linked, first, taken });
    const answer = await send(bookingLinkAvailability({ query: { from: first, to: addDays(first, 6) } }));
    if (answer.status === 200 && answer.data) {
      const slots = slotsByDay(answer.data.slots, linked.booking.timezone);
      // A later week asked for meanwhile wins. The first day with free times starts selected.
      setView((current) =>
        current.is === "pick" && current.first === first ? { ...current, slots, day: current.day ?? [...slots.keys()][0] } : current,
      );
    } else {
      otherwise(answer, () => loadWeek(linked, first, taken));
    }
  }

  function cancel(linked: LinkedBooking) {
    const again = () =>
      act(async () => {
        const answer = await send(
          bookingLinkCancel({ body: { booking_id: linked.booking.id, refund_pct: linked.engine.refund_pct } }),
        );
        if (answer.status === 200 && answer.data) setView({ is: "cancelled", linked: answer.data });
        else await afterWrite(answer, again);
      });
    return again();
  }

  function move(pick: Pick) {
    const again = () =>
      act(async () => {
        const { linked, chosen } = pick;
        const answer = await send(
          bookingLinkReschedule({
            body: { booking_id: linked.booking.id, starts_at: chosen!, reschedule_count: linked.engine.reschedule_count },
          }),
        );
        if (answer.status === 200 && answer.data) setView({ is: "moved", linked: answer.data });
        else await afterWrite(answer, again, pick);
      });
    return again();
  }

  function confirmConsents(linked: LinkedBooking) {
    const again = () =>
      act(async () => {
        const answer = await send(bookingLinkConsents({ body: { booking_id: linked.booking.id } }));
        if (answer.status === 200 && answer.data) setView({ is: "booking", linked: answer.data, note: "consentDone" });
        else await afterWrite(answer, again);
      });
    return again();
  }

  const problem = failed && (
    <>
      <Banner tone="error">{t("error")}</Banner>
      <button
        className={`${styles.button} ${styles.secondary}`}
        type="button"
        onClick={() => {
          const again = failed;
          setFailed(null);
          again();
        }}
      >
        {t("retry")}
      </button>
    </>
  );

  if (view.is === "opening") {
    return (
      <>
        <h1 className={styles.heading}>{t("title")}</h1>
        <p className={styles.lede} role="status">
          {t("opening")}
        </p>
        {problem}
      </>
    );
  }

  if (view.is === "notActive") {
    return (
      <Outcome icon="expired" title={t("notActiveTitle")} lede={t("notActiveLede")}>
        {null}
      </Outcome>
    );
  }

  if (view.is === "another") {
    return (
      <Outcome icon="calendar" title={t("anotherTitle")} lede={t("anotherLede")}>
        {null}
      </Outcome>
    );
  }

  const { linked } = view;
  const { booking, engine } = linked;
  const zone = booking.timezone;
  const when = (instant: string) => t("when", { ...localWhen(instant, zone, dateLocale(locale)), zone: zoneCity(zone) });
  const refund = refundMessage(engine);
  const refundText = refund === "pending" ? t("copy.pending") : t(refund);

  if (view.is === "cancelled") {
    return (
      <Outcome icon="done" title={t("cancelledTitle")} lede={t("cancelledLede")}>
        <p className={styles.hint}>{refundText}</p>
      </Outcome>
    );
  }

  if (view.is === "moved") {
    return (
      <Outcome icon="done" title={t("movedTitle", { when: when(booking.starts_at) })} lede={t("movedLede")}>
        <button className={`${styles.button} ${styles.secondary}`} type="button" onClick={() => setView({ is: "booking", linked })}>
          {t("backToBooking")}
        </button>
      </Outcome>
    );
  }

  const summary = <Summary linked={linked} when={when} />;

  if (view.is === "cancel") {
    return (
      <>
        <Heading focus key="cancel">
          {t("cancelTitle")}
        </Heading>
        {summary}
        <p className={styles.lede}>{refundText}</p>
        {problem}
        <div className={styles.stack}>
          <button
            className={`${styles.button} ${styles.primary}`}
            type="button"
            aria-disabled={busy || undefined}
            onClick={() => cancel(linked)}
          >
            {busy && <span className={styles.spinner} aria-hidden="true" />}
            {busy ? t("cancelling") : t("yesCancel")}
          </button>
          <button className={`${styles.button} ${styles.secondary}`} type="button" onClick={() => !busy && setView({ is: "booking", linked })}>
            {t("keep")}
          </button>
        </div>
      </>
    );
  }

  if (view.is === "pick") {
    const pick = view;
    if (pick.chosen) {
      return (
        <>
          <Heading focus key="move">
            {t("confirmTitle")}
          </Heading>
          <Summary linked={linked} when={when} to={pick.chosen} />
          <p className={styles.lede}>{t("changesAfter", { n: Math.max(engine.reschedules_left - 1, 0) })}</p>
          {problem}
          <form
            className={styles.stack}
            onSubmit={(event) => {
              event.preventDefault();
              move(pick);
            }}
          >
            <Submit busy={busy} busyLabel={t("moving")}>
              {t("confirm")}
            </Submit>
            <button className={`${styles.button} ${styles.secondary}`} type="button" onClick={() => !busy && setView({ ...pick, chosen: undefined })}>
              {t("back")}
            </button>
          </form>
        </>
      );
    }
    const today = localDay(new Date(), zone);
    const days = weekDays(pick.first);
    const dayLabel = (day: string, options: Intl.DateTimeFormatOptions) =>
      new Intl.DateTimeFormat(dateLocale(locale), { ...options, timeZone: "UTC" }).format(new Date(`${day}T12:00:00Z`));
    const times = pick.day ? (pick.slots?.get(pick.day) ?? []) : [];
    return (
      <div className={`${styles.col} ${styles.picker}`}>
        <Heading focus key="pick">
          {t("pickTitle")}
        </Heading>
        {pick.taken && <Banner tone="note">{t("slotTaken")}</Banner>}
        {problem}
        <div className={styles.screenHead}>
          <button
            className={`${styles.button} ${styles.secondary} ${styles.small}`}
            type="button"
            disabled={pick.first <= today}
            onClick={() => loadWeek(linked, addDays(pick.first, -7))}
          >
            {t("previousWeek")}
          </button>
          <button className={`${styles.button} ${styles.secondary} ${styles.small}`} type="button" onClick={() => loadWeek(linked, addDays(pick.first, 7))}>
            {t("nextWeek")}
          </button>
        </div>
        {!pick.slots ? (
          <p className={styles.lede} role="status">
            {t("loadingTimes")}
          </p>
        ) : pick.slots.size === 0 ? (
          <p className={styles.hint} role="status">
            {t("noTimes")}
          </p>
        ) : (
          <div className={styles.days}>
            {days.map((day) => (
              <button
                key={day}
                className={`${styles.button} ${day === pick.day ? styles.primary : styles.secondary}`}
                type="button"
                disabled={!pick.slots!.has(day)}
                aria-pressed={day === pick.day}
                aria-label={dayLabel(day, { weekday: "long", day: "numeric", month: "long" })}
                onClick={() => setView({ ...pick, day })}
              >
                <span>{dayLabel(day, { weekday: "short" })}</span>
                <span>{dayLabel(day, { day: "numeric" })}</span>
              </button>
            ))}
          </div>
        )}
        {times.length > 0 && <p className={styles.hint}>{t("timesIn", { zone: zoneCity(zone) })}</p>}
        {times.length > 0 && (
          <div className={styles.slots} role="group" aria-label={t("timesOn", { day: dayLabel(pick.day!, { weekday: "long", day: "numeric", month: "long" }) })}>
            {times.map((slot) => (
              <button key={slot} className={`${styles.button} ${styles.secondary}`} type="button" onClick={() => setView({ ...pick, chosen: slot })}>
                {localWhen(slot, zone, dateLocale(locale)).time}
              </button>
            ))}
          </div>
        )}
        <button className={`${styles.button} ${styles.secondary}`} type="button" onClick={() => setView({ is: "booking", linked })}>
          {t("back")}
        </button>
      </div>
    );
  }

  // Screen 5: the booking. The business name and the cancellation text are text someone typed: only ever rendered as text.
  const pending = booking.status === "pending";
  const copy = copyMessage(engine.copy_key);
  const purposes = problemList(
    linked.pending_consents.map((purpose) => t(`purposes.${purpose}`)),
    locale,
  );
  return (
    <>
      <Heading focus key="booking">
        {booking.business}
      </Heading>
      <p className={styles.lede}>{pending ? t("waiting", { business: booking.business }) : t("confirmed")}</p>
      {view.note && view.note !== "consentDone" && <Banner tone="note">{t(view.note)}</Banner>}
      {problem}
      {summary}
      {booking.cancellation_policy_text && <p className={styles.hint}>{booking.cancellation_policy_text}</p>}
      {engine.free_until ? (
        <p className={styles.hint}>
          {t("freeUntil", { ...localWhen(engine.free_until, zone, dateLocale(locale)), move: engine.can_reschedule ? "yes" : "no" })}
        </p>
      ) : (
        copy && <p className={styles.hint}>{t(`copy.${copy}`)}</p>
      )}
      {pending && <p className={styles.hint}>{t("pendingNote", { business: booking.business })}</p>}
      <div className={styles.stack}>
        {engine.can_cancel && (
          <button className={`${styles.button} ${styles.secondary}`} type="button" onClick={() => !busy && setView({ is: "cancel", linked })}>
            {t("cancel")}
          </button>
        )}
        {engine.can_reschedule && (
          <button
            className={`${styles.button} ${styles.secondary}`}
            type="button"
            onClick={() => !busy && loadWeek(linked, localDay(new Date(), zone))}
          >
            {t("reschedule", { n: engine.reschedules_left })}
          </button>
        )}
        {!engine.can_cancel && !engine.can_reschedule && <p className={styles.hint}>{t("noChanges", { business: booking.business })}</p>}
      </div>
      {linked.pending_consents.length > 0 && (
        <div className={styles.empty}>
          <span>{t("consentAsk", { business: booking.business, purposes })}</span>
          <button
            className={`${styles.button} ${styles.primary}`}
            type="button"
            aria-disabled={busy || undefined}
            onClick={() => confirmConsents(linked)}
          >
            {busy && <span className={styles.spinner} aria-hidden="true" />}
            {busy ? t("confirming") : t("consentConfirm")}
          </button>
        </div>
      )}
      {view.note === "consentDone" && <Banner tone="info">{t("consentDone")}</Banner>}
    </>
  );
}

/** The booking itself: what, when, with whom and for how much. Never the client's own details. */
function Summary({ linked, when, to }: { linked: LinkedBooking; when(instant: string): string; to?: string }) {
  const t = useTranslations("BookingLink");
  const locale = useLocale();
  const { booking } = linked;
  const service = serviceName(booking.service_name, locale as Locale, locale as Locale);
  const moved = booking.original_starts_at !== null && new Date(booking.original_starts_at).getTime() !== new Date(booking.starts_at).getTime();
  return (
    <ul className={styles.list}>
      <li className={styles.rowStatic}>
        <span className={styles.rowMain}>
          <span className={styles.rowTitle}>{service}</span>
          <span className={styles.rowMeta}>{to ? `${when(booking.starts_at)} → ${when(to)}` : when(booking.starts_at)}</span>
          <span className={styles.rowMeta}>
            {booking.worker_display_name ? t("with", { name: booking.worker_display_name }) : t("withTeam")}
            {" · "}
            {formatMoney(booking.price.amount_minor, booking.price.currency, locale)}
          </span>
          {moved && <span className={styles.rowMeta}>{t("originally", { when: when(booking.original_starts_at!) })}</span>}
        </span>
      </li>
    </ul>
  );
}
