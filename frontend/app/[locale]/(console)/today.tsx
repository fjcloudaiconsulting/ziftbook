"use client";

import { useLocale, useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import {
  type AgendaOut,
  bookingApprovalsList,
  bookingApprovalsRange,
  bookingApprovalsUpdate,
  type PendingOut,
  timeOffRange,
  type TimeOffRangeOut,
} from "@/api-client";
import { Link } from "@/i18n/navigation";
import { canAnswerRequests, dateLocale, type Role, showPublishStep, todayLabel } from "@/lib/console";
import { formatMoney } from "@/lib/money";
import { type NameMap, type Locale, serviceName } from "@/lib/services";
import { listWindow, localTime } from "@/lib/time-off";
import { type Answer, agendaPhase, applyAnswers, blockLabel, dayRelation, declineBody, expiryLabel, leaveQueue, mergeAgenda, nowLineAt, relativeAgo, sortQueue, todayEmpty } from "@/lib/today";

import { SignedOutBanner, useConsole } from "../_ui/console";
import styles from "../_ui/console.module.css";
import { Banner, FieldError, Heading, Mark, problem } from "../_ui/parts";
import uiStyles from "../_ui/ui.module.css";

/** Icon, catalog key and style of a status chip: shape, icon and word together, never colour alone.
 * One map for every status the API can send (Today's list and the calendar's panel both read it). */
export const CHIPS = {
  completed: { icon: "✓", word: "statusCompleted", style: "" },
  no_show: { icon: "✕", word: "statusNoShow", style: styles.chipOff },
  confirmed: { icon: "●", word: "statusConfirmed", style: styles.chipOk },
  pending: { icon: "◷", word: "statusWaiting", style: styles.chipWait },
  awaiting_payment: { icon: "◷", word: "statusAwaitingPayment", style: styles.chipWait },
  declined: { icon: "–", word: "statusDeclined", style: styles.chipOff },
  cancelled_by_merchant: { icon: "–", word: "statusCancelled", style: styles.chipOff },
  cancelled_by_client: { icon: "–", word: "statusCancelledByClient", style: styles.chipOff },
  expired: { icon: "–", word: "statusExpired", style: styles.chipOff },
} as const;

/** The chip for a status, or undefined for one this build doesn't know: never index CHIPS directly. */
export function chipOf(status: string): (typeof CHIPS)[keyof typeof CHIPS] | undefined {
  return Object.hasOwn(CHIPS, status) ? CHIPS[status as keyof typeof CHIPS] : undefined;
}

type Row = { declining: boolean; message: string; busy: boolean; error: string | null };
const BLANK: Row = { declining: false, message: "", busy: false, error: null };

/** `quiet`: another skeleton on the page already announces "Loading", so the page has one live region. */
export function Skeleton({ quiet }: { quiet?: boolean }) {
  const t = useTranslations("Console.today");
  return (
    <div aria-busy="true">
      {!quiet && (
        <p className={uiStyles.srOnly} role="status">
          {t("loading")}
        </p>
      )}
      <div className={uiStyles.stack}>
        <div className={styles.skeleton} />
        <div className={styles.skeleton} />
        <div className={styles.skeleton} />
      </div>
    </div>
  );
}

export function LoadFailure({ failure, onRetry }: { failure: ReturnType<typeof problem>; onRetry(): void }) {
  const form = useTranslations("Form");
  return (
    <>
      <Banner tone="error">{form(failure)}</Banner>
      <button className={uiStyles.textButton} type="button" onClick={onRetry}>
        {form("tryAgain")}
      </button>
    </>
  );
}

function Steps() {
  const { session, settings } = useConsole();
  const t = useTranslations("Console.today");
  return (
    <>
      <div className={uiStyles.empty}>
        <Mark icon="calendar" />
        <strong>{t("ownerEmptyTitle")}</strong>
        <span>{t("ownerEmptyBody")}</span>
      </div>
      <ol className={styles.steps} aria-label={t("steps")}>
        <li>
          <span className={styles.stepMark} aria-hidden="true">
            {1}
          </span>
          <Link className={uiStyles.textButton} href="/opening-hours">
            {t("step1")}
          </Link>
        </li>
        <li>
          <span className={styles.stepMark} aria-hidden="true">
            {2}
          </span>
          <Link className={uiStyles.textButton} href="/services">
            {t("step2")}
          </Link>
        </li>
        <li>
          <span className={styles.stepMark} aria-hidden="true">
            {3}
          </span>
          <Link className={uiStyles.textButton} href={`/team/${session.member_id}`}>
            {t("step3")}
          </Link>
        </li>
        <li>
          <span className={styles.stepMark} aria-hidden="true">
            {4}
          </span>
          <Link className={uiStyles.textButton} href="/team">
            {t("step4")}
          </Link>
        </li>
        {showPublishStep(settings.published) && (
          <li>
            <span className={styles.stepMark} aria-hidden="true">
              {5}
            </span>
            <Link className={uiStyles.textButton} href="/settings">
              {t("step5")}
            </Link>
          </li>
        )}
      </ol>
      <p className={styles.stepsHint}>{t("stepsHint")}</p>
    </>
  );
}

export function Today() {
  const { session, settings, call, setPendingCount } = useConsole();
  const t = useTranslations("Console.today");
  const nav = useTranslations("Console.nav");
  const person = useTranslations("Console.person");
  const errorsT = useTranslations("Console.errors");
  const form = useTranslations("Form");
  const locale = useLocale();
  const tz = settings.timezone;
  const isOwner = session.role === "owner";
  // Off (ZIF-143): the queue is read only, a Waiting chip where Accept and Decline sit.
  const canAnswer = canAnswerRequests(session.role as Role, settings.workers_answer_requests);

  // "Now" is read once per render pass, and never ticks: the "now" line and the expiry days only
  // move when something reloads, which is enough for a screen people leave open for minutes.
  const [now] = useState(() => new Date());
  const dayWindow = listWindow(now, tz, 1);
  const label = todayLabel(now, tz, locale);

  const [queue, setQueue] = useState<PendingOut[] | null>(null);
  const [agenda, setAgenda] = useState<AgendaOut[] | null>(null);
  const [blocks, setBlocks] = useState<TimeOffRangeOut[] | null>(null);
  const [queueFailure, setQueueFailure] = useState<ReturnType<typeof problem> | null>(null);
  const [agendaFailure, setAgendaFailure] = useState<ReturnType<typeof problem> | null>(null);
  // Decline-open, the typed message, busy and the row's own error are keyed by booking id, so a refetch keeps them.
  const [rows, setRows] = useState<Record<string, Row>>({});
  const [showAll, setShowAll] = useState(false);
  const [status, setStatus] = useState("");
  const [signedOut, setSignedOut] = useState(false);
  const answers = useRef(new Map<string, Answer>());
  const submitting = useRef(new Set<string>());
  // An element id to focus after the next render, or "heading" for the section's (else the page's) heading.
  const focusTarget = useRef<string | null>(null);
  const queueHead = useRef<HTMLHeadingElement>(null);
  const queueList = useRef<HTMLUListElement>(null);

  useEffect(() => {
    const target = focusTarget.current;
    if (!target) return;
    focusTarget.current = null;
    (target === "heading" ? (queueHead.current ?? document.querySelector<HTMLElement>("h1")) : document.getElementById(target))?.focus();
  });

  function loadQueue() {
    // ponytail: limit=100, so more than 100 live requests shows the soonest-starting 100 (the API
    // orders by start). Upgrade: a server ORDER BY expires_at.
    call(() => bookingApprovalsList({ query: { limit: 100 } })).then((outcome) => {
      if (outcome.status !== 200 || !outcome.data) return setQueueFailure(problem(outcome));
      const next = sortQueue(applyAnswers(outcome.data, answers.current, "queue"));
      // A refetch that takes away the row the person is on must not strand focus on nothing.
      const focused = document.activeElement?.closest<HTMLElement>("[data-booking]");
      if (focused && !next.some((booking) => booking.id === focused.dataset.booking)) focusTarget.current = "heading";
      setQueueFailure(null);
      setQueue(next);
    });
  }

  function loadAgenda() {
    Promise.all([call(() => bookingApprovalsRange({ query: dayWindow })), call(() => timeOffRange({ query: dayWindow }))]).then(([bookings, off]) => {
      if (bookings.status !== 200 || !bookings.data || off.status !== 200 || !off.data) {
        return setAgendaFailure(problem(bookings.status !== 200 ? bookings : off));
      }
      setAgendaFailure(null);
      setAgenda(applyAnswers(bookings.data, answers.current, "agenda"));
      setBlocks(off.data);
    });
  }

  // The nav badge follows the queue, whichever update (load or answer) last changed it.
  useEffect(() => {
    if (queue) setPendingCount(queue.length);
  }, [queue, setPendingCount]);

  useEffect(() => {
    loadQueue();
    loadAgenda();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function patchRow(id: string, patch: Partial<Row>) {
    setRows((current) => ({ ...current, [id]: { ...(current[id] ?? BLANK), ...patch } }));
  }

  function openDecline(id: string) {
    patchRow(id, { declining: true });
    focusTarget.current = `message-${id}`;
  }

  function keepIt(id: string) {
    patchRow(id, { declining: false });
    focusTarget.current = `decline-${id}`;
  }

  /** The row leaves the queue. Focus goes to the next visible row's Accept, else the previous one's,
   * else the heading: worked out from the page itself, since which rows show depends on the screen's width. */
  function leave(booking: PendingOut, answer: Answer) {
    const rowEls = [...(queueList.current?.querySelectorAll<HTMLElement>("[data-booking]") ?? [])];
    const at = rowEls.findIndex((el) => el.dataset.booking === booking.id);
    const usable = (el: HTMLElement) => el.offsetParent !== null && el.querySelector("[data-accept]") !== null;
    const neighbour = [...rowEls.slice(at + 1), ...rowEls.slice(0, at).reverse()].find(usable);
    focusTarget.current = neighbour ? `accept-${neighbour.dataset.booking}` : "heading";
    answers.current.set(booking.id, answer);
    setQueue((current) => leaveQueue(current, answers.current));
    // The agenda follows the answer: an accepted booking is confirmed there now, a declined one is gone.
    if (answer !== "stale") setAgenda((current) => applyAnswers(current ?? [], answers.current, "agenda"));
  }

  async function answer(booking: PendingOut, to: "confirmed" | "declined") {
    if (submitting.current.has(booking.id)) return;
    submitting.current.add(booking.id);
    // Empty the live region first: the same text twice in a row isn't announced.
    setStatus("");
    patchRow(booking.id, { busy: true, error: null });
    try {
      const body = to === "confirmed" ? ({ status: "confirmed" } as const) : declineBody(rows[booking.id]?.message ?? "");
      const outcome = await call(() => bookingApprovalsUpdate({ path: { booking_id: booking.id }, body }), { write: true });
      if (outcome.status === 200) {
        leave(booking, to);
        setStatus(t(to === "confirmed" ? "accepted" : "declined", { client: booking.client_name, when: whenLabel(booking.starts_at) }));
      } else if (outcome.status === 401) {
        setSignedOut(true);
      } else if (outcome.status === 409) {
        // Answered elsewhere, or it expired meanwhile: say so, drop the row, and read the truth again.
        leave(booking, "stale");
        setStatus(t("stale"));
        loadQueue();
        loadAgenda();
      } else if (outcome.status === 403 && outcome.code === "owner_only") {
        patchRow(booking.id, { error: errorsT("ownerOnly") });
      } else {
        patchRow(booking.id, { error: form(problem(outcome)) });
      }
    } catch {
      patchRow(booking.id, { error: form("unexpected") });
    } finally {
      patchRow(booking.id, { busy: false });
      submitting.current.delete(booking.id);
    }
  }

  const dateFormat = new Intl.DateTimeFormat(dateLocale(locale), { weekday: "short", day: "numeric", month: "short", timeZone: tz });
  /** "Today 15:30", "Tomorrow 10:00", else "Thu 1 Oct 10:00": all in the business zone. */
  function whenLabel(instant: string): string {
    const time = localTime(instant, tz);
    const relation = dayRelation(instant, now, tz);
    if (relation === "today") return t("whenToday", { time });
    if (relation === "tomorrow") return t("whenTomorrow", { time });
    return t("whenLater", { date: dateFormat.format(new Date(instant)), time });
  }
  const minutes = (start: string, end: string) => t("minutes", { n: Math.round((new Date(end).getTime() - new Date(start).getTime()) / 60_000) });
  const worker = (name: string | null) => t("withWorker", { name: name ?? person("nameNotSet") });
  const relative = new Intl.RelativeTimeFormat(dateLocale(locale), { numeric: "auto" });
  const serviceOf = (b: { service_name: { [key: string]: string } }) => serviceName(b.service_name as NameMap, locale as Locale, settings.language as Locale);

  function expiry(expiresAt: string) {
    const { when, time } = expiryLabel(expiresAt, now, tz);
    const key = when === "today" ? "expiresToday" : when === "tomorrow" ? "expiresTomorrow" : "expiresLater";
    return t.rich(canAnswer ? key : `${key}Owner`, { time, date: dateFormat.format(new Date(expiresAt)), long: (chunks) => <span className={styles.desktopOnly}>{chunks}</span> });
  }

  // The agenda shows when its own data is in (or failed), whatever the queue's load did.
  const items = agenda !== null && blocks !== null ? mergeAgenda(agenda, blocks, tz, dayWindow, { role: isOwner ? "owner" : "worker", memberId: session.member_id }) : null;
  const line = nowLineAt(items ?? [], now.getTime());
  // Phone: the same action at the bottom of the screen (desktop has it beside the heading).
  const newPhone = (
    <Link className={`${uiStyles.button} ${uiStyles.primary} ${styles.newPhone}`} href="/calendar?panel=new">
      {t("newBooking")}
    </Link>
  );
  const nothing = todayEmpty({ queue, items, failed: Boolean(queueFailure || agendaFailure) });

  const head = (
    <>
      <div className={styles.todayHead}>
        <Heading focus>{nav("today")}</Heading>
        <Link className={`${uiStyles.button} ${uiStyles.primary} ${uiStyles.small} ${styles.newDesktop}`} href="/calendar?panel=new">
          {t("newBooking")}
        </Link>
      </div>
      <p className={uiStyles.lede}>{label}</p>
      <p className={uiStyles.srOnly} role="status" aria-live="polite">
        {status}
      </p>
      {/* The same line for sighted users (calendar-design notes: the row leaves with a short
          "Accepted: ..." line). aria-hidden: the live region above already announces it. */}
      {status && (
        <p className={uiStyles.hint} aria-hidden="true">
          {status}
        </p>
      )}
      {signedOut && <SignedOutBanner />}
    </>
  );

  if (nothing && !isOwner) {
    return (
      <>
        {head}
        <div className={uiStyles.empty}>
          <Mark icon="calendar" />
          <strong>{t("workerEmptyTitle")}</strong>
          <span>{t("workerEmptyBody")}</span>
          <Link className={`${uiStyles.button} ${uiStyles.secondary}`} href="/my-hours">
            {t("checkHours")}
          </Link>
        </div>
        {newPhone}
      </>
    );
  }

  if (nothing && !settings.published) {
    return (
      <>
        {head}
        <Steps />
        {newPhone}
      </>
    );
  }

  if (nothing) {
    return (
      <>
        {head}
        <div className={uiStyles.empty}>
          <Mark icon="calendar" />
          <strong>{t("nothingToday")}</strong>
          <span>{t("newRequestsHint")}</span>
        </div>
        {newPhone}
      </>
    );
  }

  return (
    <>
      {head}
      {(queueFailure || queue === null || queue.length > 0) && (
        <section className={styles.todaySection} aria-labelledby="queue-head">
          <h2 id="queue-head" className={styles.sectionHead} ref={queueHead} tabIndex={-1}>
            {canAnswer ? t("queueTitle") : t("queueTitleOwner")}
            {canAnswer && queue && queue.length > 0 && <span className={styles.count}>{queue.length}</span>}
          </h2>
          {!canAnswer && <p className={`${uiStyles.hint} ${styles.queueHint}`}>{t("queueHintOwner")}</p>}
          {queueFailure ? (
            <LoadFailure failure={queueFailure} onRetry={loadQueue} />
          ) : queue === null ? (
            <Skeleton />
          ) : (
            <>
              <ul ref={queueList} className={`${styles.list} ${showAll ? styles.queueOpen : ""}`}>
                {queue.map((booking, index) => {
                  const row = rows[booking.id] ?? BLANK;
                  const created = relativeAgo(booking.created_at, now);
                  const meta = [
                    isOwner ? worker(booking.worker_display_name) : null,
                    formatMoney(booking.price.amount_minor, booking.price.currency, locale),
                  ].filter(Boolean);
                  return (
                    <li key={booking.id} data-booking={booking.id} className={`${styles.req} ${index >= 2 ? styles.reqExtra : ""}`}>
                      <div className={styles.when}>
                        {whenLabel(booking.starts_at)}
                        <small>{minutes(booking.starts_at, booking.ends_at)}</small>
                      </div>
                      <div className={styles.what}>
                        <b>{serviceOf(booking)}</b> · {booking.client_name}
                        <span>
                          {meta.join(" · ")}
                          <span className={styles.desktopOnly}> · {t("bookedOnline", { relative: relative.format(created.value, created.unit) })}</span>
                        </span>
                      </div>
                      {!canAnswer && (
                        <span className={`${styles.chip} ${CHIPS.pending.style} ${styles.reqChip}`}>
                          <span aria-hidden="true">{CHIPS.pending.icon}</span>
                          <span className={styles.chipWord}>{t(CHIPS.pending.word)}</span>
                        </span>
                      )}
                      {canAnswer && !row.declining && (
                        <div className={styles.acts}>
                          <button
                            id={`accept-${booking.id}`}
                            data-accept
                            className={`${uiStyles.button} ${uiStyles.primary} ${uiStyles.small}`}
                            type="button"
                            aria-disabled={row.busy || undefined}
                            onClick={() => answer(booking, "confirmed")}
                          >
                            {row.busy ? form("sending") : t("accept")}
                          </button>
                          <button
                            id={`decline-${booking.id}`}
                            className={`${uiStyles.button} ${uiStyles.secondary} ${uiStyles.small}`}
                            type="button"
                            aria-disabled={row.busy || undefined}
                            onClick={() => !row.busy && openDecline(booking.id)}
                          >
                            {t("decline")}
                          </button>
                        </div>
                      )}
                      <div className={styles.expires}>
                        <span aria-hidden="true">{"◷ "}</span>
                        {expiry(booking.expires_at)}
                      </div>
                      {row.declining && (
                        <div className={styles.declining}>
                          <div className={uiStyles.field}>
                            <label className={uiStyles.label} htmlFor={`message-${booking.id}`}>
                              {t("declineLabel", { client: booking.client_name })}
                            </label>
                            <div className={uiStyles.input}>
                              <textarea
                                id={`message-${booking.id}`}
                                maxLength={1000}
                                value={row.message}
                                aria-describedby={`message-${booking.id}-hint`}
                                onChange={(event) => patchRow(booking.id, { message: event.target.value })}
                              />
                            </div>
                            <p className={uiStyles.hint} id={`message-${booking.id}-hint`}>
                              {t("declineHint", { client: booking.client_name })}
                            </p>
                          </div>
                          <div className={styles.acts}>
                            <button
                              className={`${uiStyles.button} ${uiStyles.primary} ${uiStyles.small}`}
                              type="button"
                              aria-disabled={row.busy || undefined}
                              onClick={() => answer(booking, "declined")}
                            >
                              {row.busy ? form("sending") : t("declineConfirm")}
                            </button>
                            <button
                              className={`${uiStyles.button} ${uiStyles.secondary} ${uiStyles.small}`}
                              type="button"
                              aria-disabled={row.busy || undefined}
                              onClick={() => !row.busy && keepIt(booking.id)}
                            >
                              {t("keepIt")}
                            </button>
                          </div>
                        </div>
                      )}
                      {row.error && (
                        <div className={styles.rowError}>
                          <FieldError id={`error-${booking.id}`}>{row.error}</FieldError>
                        </div>
                      )}
                    </li>
                  );
                })}
              </ul>
              {queue.length > 2 && !showAll && (
                <button className={`${uiStyles.textButton} ${styles.showMore}`} type="button" onClick={() => setShowAll(true)}>
                  {t("showMore", { count: queue.length - 2 })}
                </button>
              )}
            </>
          )}
        </section>
      )}

      <section className={styles.todaySection} aria-labelledby="agenda-head">
        <h2 id="agenda-head" className={styles.sectionHead}>
          {t("agendaTitle")}
        </h2>
        {agendaFailure ? (
          <LoadFailure failure={agendaFailure} onRetry={loadAgenda} />
        ) : items === null ? (
          <Skeleton quiet={queue === null && !queueFailure} />
        ) : items.length === 0 ? (
          <p className={uiStyles.hint}>{t("nothingToday")}</p>
        ) : (
          <ol className={styles.agenda}>
            {items.map((item, index) => {
              const past = agendaPhase(item, now.getTime()) === "past" ? styles.agendaPast : "";
              const nowLine = index === line && (
                <li key="now" className={styles.nowLine}>
                  <span>{t("nowLine", { time: localTime(now.toISOString(), tz) })}</span>
                </li>
              );
              if (item.kind === "block") {
                const { block } = item;
                const when = blockLabel(item, dayWindow, now, tz);
                return [
                  nowLine,
                  <li key={item.key} className={`${styles.agendaBlock} ${past}`}>
                    <div className={styles.agendaTime}>
                      {when.kind === "allDay" ? t("allDay") : when.kind === "until" ? t("until", { time: when.time }) : when.time}
                      {when.kind === "timed" && <small>{t("minutes", { n: when.minutes })}</small>}
                      {when.kind === "from" && (
                        <small>
                          {when.endsOn === "tomorrow"
                            ? t("untilTomorrow", { time: when.endTime })
                            : t("untilLater", { date: dateFormat.format(new Date(when.endInstant)), time: when.endTime })}
                        </small>
                      )}
                    </div>
                    <div className={styles.agendaWhat}>
                      {block.reason ? t("blockedReason", { reason: block.reason }) : t("blocked")}
                      {isOwner && block.member_name && <span>{block.member_name}</span>}
                    </div>
                    <span />
                  </li>,
                ];
              }
              const { booking } = item;
              const chip = chipOf(booking.status);
              const meta = [isOwner ? worker(booking.worker_display_name) : null, booking.source === "merchant" ? t("walkIn") : null].filter(Boolean);
              return [
                nowLine,
                <li key={item.key} className={past}>
                  <div className={styles.agendaTime}>
                    {localTime(booking.starts_at, tz)}
                    <small>{minutes(booking.starts_at, booking.ends_at)}</small>
                  </div>
                  <div className={styles.agendaWhat}>
                    {serviceOf(booking)} · {booking.client_name}
                    {meta.length > 0 && <span>{meta.join(" · ")}</span>}
                  </div>
                  {chip ? (
                    <span className={`${styles.chip} ${chip.style}`}>
                      <span aria-hidden="true">{chip.icon}</span>
                      <span className={styles.chipWord}>{t(chip.word)}</span>
                    </span>
                  ) : (
                    <span />
                  )}
                </li>,
              ];
            })}
            {line === items.length && (
              <li className={styles.nowLine}>
                <span>{t("nowLine", { time: localTime(now.toISOString(), tz) })}</span>
              </li>
            )}
          </ol>
        )}
        <Link className={`${uiStyles.textButton} ${styles.openCalendar}`} href="/calendar">
          {t("openCalendar")}
        </Link>
      </section>
      {newPhone}
    </>
  );
}
