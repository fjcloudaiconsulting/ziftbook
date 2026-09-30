"use client";

import { useLocale, useTranslations } from "next-intl";
import { type MouseEvent, useEffect, useRef, useState } from "react";

import { type BookingDetailOut, bookingApprovalsRead, bookingApprovalsUpdate } from "@/api-client";
import { Link } from "@/i18n/navigation";
import { type Action, actionsFor, historyLabel, whenParts } from "@/lib/calendar";
import { dateLocale } from "@/lib/console";
import { formatMoney } from "@/lib/money";
import { type Locale, type NameMap, serviceName } from "@/lib/services";
import { localTime } from "@/lib/time-off";
import { declineBody, expiryLabel } from "@/lib/today";

import { SignedOutBanner, useConsole } from "../../_ui/console";
import styles from "../../_ui/console.module.css";
import { Banner, FieldError, problem } from "../../_ui/parts";
import uiStyles from "../../_ui/ui.module.css";
import { CHIPS, chipOf, LoadFailure, Skeleton } from "../today";
import css from "./calendar.module.css";

const TARGET = { accept: "confirmed", completed: "completed", no_show: "no_show", restore: "confirmed", cancel: "cancelled_by_merchant" } as const;
const DONE = {
  accept: "doneAccepted",
  decline: "doneDeclined",
  completed: "doneCompleted",
  no_show: "doneNoShow",
  restore: "doneRestored",
  cancel: "doneCancelled",
} as const;

type Load = { id: string; status: number; failure: ReturnType<typeof problem> };

export function BookingDetail({
  id,
  now,
  closeHref,
  onClose,
  onChanged,
}: {
  id: string;
  now: Date;
  closeHref: string;
  onClose(event: MouseEvent<HTMLAnchorElement>): void;
  /** The booking changed: the calendar reads its window again. */
  onChanged(): void;
}) {
  const { session, settings, call, setPendingCount } = useConsole();
  const t = useTranslations("Console.calendar");
  const today = useTranslations("Console.today");
  const person = useTranslations("Console.person");
  const errorsT = useTranslations("Console.errors");
  const form = useTranslations("Form");
  const locale = useLocale();
  const tz = settings.timezone;
  const role = session.role === "owner" ? "owner" : "worker";

  const [detail, setDetail] = useState<BookingDetailOut | null>(null);
  const [load, setLoad] = useState<Load | null>(null);
  const [declining, setDeclining] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState("");
  const [signedOut, setSignedOut] = useState(false);
  // One sequence for the first read and every refetch after a change: a slow older answer never wins.
  const seq = useRef(0);
  const submitting = useRef(false);
  const focusTarget = useRef<string | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);

  // Opening (or switching to) a booking moves focus to the panel: the phone list behind it is hidden.
  useEffect(() => {
    heading.current?.focus();
  }, []);

  useEffect(() => {
    const target = focusTarget.current;
    if (!target) return;
    focusTarget.current = null;
    (target === "heading" ? heading.current : document.getElementById(target))?.focus();
  });

  function read(): Promise<void> {
    const mine = ++seq.current;
    return call(() => bookingApprovalsRead({ path: { booking_id: id } })).then((outcome) => {
      if (mine !== seq.current) return;
      if (outcome.status === 200 && outcome.data) {
        setDetail(outcome.data);
        setLoad(null);
      } else {
        setLoad({ id, status: outcome.status, failure: problem(outcome) });
      }
    });
  }

  useEffect(() => {
    read();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  async function act(action: Action) {
    // Only ever the booking the panel is showing, and only once that one is what has loaded.
    if (!detail || detail.id !== id || submitting.current) return;
    submitting.current = true;
    setStatus("");
    setError(null);
    setBusy(true);
    const source = detail.status;
    try {
      const body = action === "decline" ? declineBody(message) : ({ status: TARGET[action as keyof typeof TARGET] } as const);
      const outcome = await call(() => bookingApprovalsUpdate({ path: { booking_id: id }, body }), { write: true });
      if (outcome.status === 200) {
        if (source === "pending") setPendingCount((count) => (count === null ? count : Math.max(0, count - 1)));
        setDeclining(false);
        setCancelling(false);
        setMessage("");
        setStatus(t(DONE[action]));
        focusTarget.current = "heading";
        await read();
        onChanged();
      } else if (outcome.status === 401) {
        setSignedOut(true);
      } else if (outcome.status === 409) {
        setStatus(t("changed"));
        await read();
        onChanged();
      } else if (outcome.status === 403 && outcome.code === "owner_only") {
        setError(errorsT("ownerOnly"));
      } else {
        setError(form(problem(outcome)));
      }
    } catch {
      setError(form("unexpected"));
    } finally {
      setBusy(false);
      submitting.current = false;
    }
  }

  function openDecline() {
    setDeclining(true);
    focusTarget.current = "detail-message";
  }
  function keepDecline() {
    setDeclining(false);
    focusTarget.current = "detail-decline";
  }
  function openCancel() {
    setCancelling(true);
    focusTarget.current = "detail-cancel-title";
  }
  function keepBooking() {
    setCancelling(false);
    focusTarget.current = "detail-cancel";
  }

  const ready = detail !== null && detail.id === id ? detail : null;
  const failed = load !== null && load.id === id ? load : null;
  const dateFormat = new Intl.DateTimeFormat(dateLocale(locale), { weekday: "short", day: "numeric", month: "short", timeZone: tz });
  const dayOf = (iso: string) => dateFormat.format(new Date(`${iso}T12:00:00Z`));
  const stamp = (instant: string) => `${dateFormat.format(new Date(instant))} ${localTime(instant, tz)}`;

  function when(d: BookingDetailOut): string {
    const p = whenParts(d.starts_at, d.ends_at, now, tz);
    const values = { start: p.startTime, end: p.endTime, date: dateFormat.format(new Date(d.starts_at)), endDate: dateFormat.format(new Date(d.ends_at)) };
    if (p.today) return t(p.endsLater ? "whenTodayLater" : "whenToday", values);
    return t(p.endsLater ? "whenDateLater" : "whenDate", values);
  }

  function historyLine(d: BookingDetailOut, i: number) {
    const { key, params } = historyLabel(d.history, i, d.source, d.max_reschedules, tz);
    const at = d.history[i].at;
    const moved = key === "historyMoved";
    const text = t(key, {
      actor: (params.actor as string | null) ?? t("aTeamMember"),
      client: d.client_name,
      from: moved ? (params.sameDay ? `${params.fromTime}` : `${dayOf(String(params.fromDate))} ${params.fromTime}`) : "",
      to: moved ? (params.sameDay ? `${params.toTime}` : `${dayOf(String(params.toDate))} ${params.toTime}`) : "",
    });
    const latest = i === d.history.length - 1;
    const waiting = latest && d.status === "pending" && !d.expired && d.history[i].event === "created";
    const small = waiting
      ? t("historyWaiting", { when: stamp(at) })
      : moved
        ? `${stamp(at)} · ${t("changesUsed", { k: Number(params.k), max: Number(params.max) })}`
        : stamp(at);
    return (
      <li key={`${i}-${d.history[i].event}`} aria-current={latest ? "true" : undefined}>
        {text}
        <small>{small}</small>
      </li>
    );
  }

  function expiryLine(d: BookingDetailOut) {
    if (!d.expires_at) return null;
    const { when: day, time } = expiryLabel(d.expires_at, now, tz);
    const key = day === "today" ? "expiresToday" : day === "tomorrow" ? "expiresTomorrow" : "expiresLater";
    return (
      <p className={css.expiry}>
        <span aria-hidden="true">{"◷ "}</span>
        {today.rich(key, { time, date: dateFormat.format(new Date(d.expires_at)), long: (chunks) => <span>{chunks}</span> })}
      </p>
    );
  }

  const button = (action: Action, label: string, variant: "primary" | "secondary", extra = "", onClick?: () => void, buttonId?: string) => (
    <button
      id={buttonId}
      className={`${uiStyles.button} ${uiStyles[variant]} ${uiStyles.small} ${extra}`}
      type="button"
      aria-disabled={busy || undefined}
      onClick={() => !busy && (onClick ?? (() => act(action)))()}
    >
      {busy && !onClick ? form("sending") : label}
    </button>
  );

  function actions(d: BookingDetailOut) {
    const list = actionsFor(d, role, now);
    if (list.length === 0) return null;
    const has = (a: Action) => list.includes(a);
    const name = d.client_name;
    return (
      <>
        {has("accept") && !declining && (
          <div className={css.split}>
            {button("accept", today("accept"), "primary")}
            {button("decline", today("decline"), "secondary", "", openDecline, "detail-decline")}
          </div>
        )}
        {has("accept") && declining && (
          <div className={css.confirm}>
            <div className={uiStyles.field}>
              <label className={uiStyles.label} htmlFor="detail-message">
                {today("declineLabel", { client: name })}
              </label>
              <div className={uiStyles.input}>
                <textarea id="detail-message" maxLength={1000} value={message} aria-describedby="detail-message-hint" onChange={(event) => setMessage(event.target.value)} />
              </div>
              <p className={uiStyles.hint} id="detail-message-hint">
                {today("declineHint", { client: name })}
              </p>
            </div>
            <div className={css.split}>
              {button("decline", today("declineConfirm"), "primary")}
              {button("decline", today("keepIt"), "secondary", "", keepDecline)}
            </div>
          </div>
        )}
        {has("accept") && expiryLine(d)}
        {(has("completed") || has("restore")) && (
          <div className={css.split}>
            {has("restore") ? button("restore", t("restoreAction"), "primary") : button("completed", t("completedAction"), "primary")}
            {has("no_show") && button("no_show", t("noShowAction"), "secondary")}
          </div>
        )}
        {d.status === "confirmed" && !has("completed") && <p className={uiStyles.hint}>{t("pastHint")}</p>}
        {has("cancel") && !cancelling && button("cancel", t("cancelAction"), "secondary", css.danger, openCancel, "detail-cancel")}
        {has("cancel") && cancelling && (
          <div className={css.confirm}>
            <h3 id="detail-cancel-title" tabIndex={-1}>
              {t("cancelTitle")}
            </h3>
            <p className={uiStyles.hint}>{d.status === "pending" || d.status === "confirmed" ? t("cancelHint", { client: d.client_name }) : t("cancelHintPlain")}</p>
            <div className={css.split}>
              {button("cancel", t("cancelAction"), "primary")}
              {button("cancel", today("keepIt"), "secondary", "", keepBooking)}
            </div>
          </div>
        )}
      </>
    );
  }

  const chip = ready ? (ready.status === "pending" && ready.expired ? CHIPS.expired : chipOf(ready.status)) : undefined;

  return (
    <aside className={css.panel} aria-labelledby="detail-title">
      <Link className={css.backLink} href={closeHref} replace scroll={false} onClick={onClose}>
        <span aria-hidden="true">{"‹ "}</span>
        {t("back")}
      </Link>
      <div className={css.panelHead}>
        <div>
          <h2 id="detail-title" className={css.panelTitle} ref={heading} tabIndex={-1}>
            {ready ? ready.client_name : t("detailTitle")}
          </h2>
          {chip && (
            <span className={`${styles.chip} ${chip.style}`}>
              <span aria-hidden="true">{chip.icon}</span>
              {today(chip.word)}
            </span>
          )}
        </div>
        <Link className={css.close} href={closeHref} replace scroll={false} aria-label={t("close")} onClick={onClose}>
          <span aria-hidden="true">{"✕"}</span>
        </Link>
      </div>
      <p className={uiStyles.srOnly} role="status" aria-live="polite">
        {status}
      </p>
      {status && (
        <p className={uiStyles.hint} aria-hidden="true">
          {status}
        </p>
      )}
      {signedOut && <SignedOutBanner />}
      {failed ? (
        failed.status === 403 || failed.status === 404 ? (
          <Banner tone="error">{failed.status === 403 ? t("detailForbidden") : t("detailGone")}</Banner>
        ) : (
          <LoadFailure failure={failed.failure} onRetry={() => read()} />
        )
      ) : !ready ? (
        <Skeleton />
      ) : (
        <>
          <dl className={css.facts}>
            <dt>{t("factWhen")}</dt>
            <dd>{when(ready)}</dd>
            <dt>{t("factService")}</dt>
            <dd>{`${serviceName(ready.service_name as NameMap, locale as Locale, settings.language as Locale)} · ${formatMoney(ready.price.amount_minor, ready.price.currency, locale)}`}</dd>
            <dt>{t("factWith")}</dt>
            <dd>{ready.worker_display_name ?? person("nameNotSet")}</dd>
            {ready.client_phone && (
              <>
                <dt>{t("factPhone")}</dt>
                <dd>
                  <a href={`tel:${ready.client_phone}`}>{ready.client_phone}</a>
                </dd>
              </>
            )}
            {ready.client_email && (
              <>
                <dt>{t("factEmail")}</dt>
                <dd>{ready.client_email}</dd>
              </>
            )}
            {ready.client_note && (
              <>
                <dt>{t("factNote")}</dt>
                <dd>{ready.client_note}</dd>
              </>
            )}
          </dl>
          {actions(ready)}
          {error && <FieldError id="detail-error">{error}</FieldError>}
          <div className={uiStyles.stack}>
            <span className={uiStyles.label}>{t("history")}</span>
            <ol className={css.historyList}>{ready.history.map((_, i) => historyLine(ready, i))}</ol>
          </div>
        </>
      )}
    </aside>
  );
}
