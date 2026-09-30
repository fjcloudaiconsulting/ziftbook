"use client";

import { useLocale, useTranslations } from "next-intl";
import { useSearchParams } from "next/navigation";
import { type CSSProperties, type MouseEvent, useEffect, useRef, useState } from "react";

import {
  type AgendaOut,
  bookingApprovalsRange,
  type MemberOut,
  membersList,
  openingHoursRead,
  type Shift,
  timeOffRange,
  type TimeOffRangeOut,
} from "@/api-client";
import { Link, useRouter } from "@/i18n/navigation";
import { closedDay, daySlices, dayWindow, hourRange, hrefFor, lanes, nowTop, parseView, type Slice, visibleDays, weekdayOf } from "@/lib/calendar";
import { dateLocale } from "@/lib/console";
import { type Locale, type NameMap, serviceName } from "@/lib/services";
import { addDaysISO, localDateISO, localTime } from "@/lib/time-off";
import { agendaPhase, blockLabel, mergeAgenda, nowLineAt } from "@/lib/today";

import { useConsole } from "../../_ui/console";
import styles from "../../_ui/console.module.css";
import { Heading, problem } from "../../_ui/parts";
import uiStyles from "../../_ui/ui.module.css";
import { chipOf, LoadFailure, Skeleton } from "../today";
import { BookingDetail } from "./booking-detail";
import css from "./calendar.module.css";

type Loaded = { key: string; bookings: AgendaOut[]; blocks: TimeOffRangeOut[]; opening: Shift[] };
type Item = ReturnType<typeof mergeAgenda<AgendaOut, TimeOffRangeOut>>[number];
type Cell = { item: Item; slice: Slice; lane: number; of: number };
type Column = { key: string; day: string; label: string; ariaLabel: string; small?: string; cells: Cell[] };

const hhmm = (minutes: number) => `${String(Math.floor((minutes % 1440) / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`;
const memberOf = (item: Item) => (item.kind === "booking" ? item.booking.worker_id : item.block.member_id);

export function Calendar() {
  const { session, settings, call } = useConsole();
  const t = useTranslations("Console.calendar");
  const today = useTranslations("Console.today");
  const nav = useTranslations("Console.nav");
  const person = useTranslations("Console.person");
  const locale = useLocale();
  const tz = settings.timezone;
  const isOwner = session.role === "owner";
  const params = useSearchParams();
  const router = useRouter();

  // The clock ticks each minute: the now line, and Completed / No-show opening in the panel.
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), 60_000);
    return () => clearInterval(timer);
  }, []);
  const todayISO = localDateISO(now, tz);

  const [members, setMembers] = useState<MemberOut[] | null>(null);
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [failure, setFailure] = useState<{ key: string; problem: ReturnType<typeof problem> } | null>(null);
  const [reload, setReload] = useState(0);
  const seq = useRef(0);
  const openedInApp = useRef(false);
  const lastBooking = useRef<string | null>(null);

  const view = parseView(params, todayISO, isOwner ? "owner" : "worker", session.member_id, members?.map((m) => m.member_id) ?? null);
  const days = visibleDays(view.view, view.date);
  const range = dayWindow(days[0], tz, days.length);
  const key = `${range.from}|${range.to}`;

  useEffect(() => {
    const mine = ++seq.current;
    Promise.all([
      call(() => bookingApprovalsRange({ query: range })),
      call(() => timeOffRange({ query: range })),
      call(() => openingHoursRead()),
      isOwner ? call(() => membersList()) : Promise.resolve(null),
    ]).then(([bookings, off, opening, team]) => {
      // A slower earlier window never overwrites a later one.
      if (mine !== seq.current) return;
      const bad = [bookings, off, opening, team].find((outcome) => outcome && (outcome.status !== 200 || !outcome.data));
      if (bad) return setFailure({ key, problem: problem(bad) });
      setFailure(null);
      if (team?.data) setMembers(team.data);
      setLoaded({ key, bookings: bookings.data!, blocks: off.data!, opening: opening.data! });
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [range.from, range.to, reload]);

  // Closing a booking returns focus to the item that opened it, or the page heading when it is gone.
  useEffect(() => {
    const before = lastBooking.current;
    lastBooking.current = view.booking;
    if (!before || view.booking) return;
    const link = [...document.querySelectorAll<HTMLElement>(`[data-open="${CSS.escape(before)}"]`)].find((el) => el.offsetParent !== null);
    (link ?? document.querySelector<HTMLElement>("h1"))?.focus();
  }, [view.booking]);

  const data = loaded && loaded.key === key ? loaded : null;
  const failed = failure && failure.key === key ? failure : null;
  const href = (patch: Parameters<typeof hrefFor>[1]) => hrefFor(view, patch);
  const weekView = view.view === "week";

  const longDay = new Intl.DateTimeFormat(dateLocale(locale), { weekday: "long", day: "numeric", month: "long", timeZone: "UTC" });
  const monthDay = new Intl.DateTimeFormat(dateLocale(locale), { day: "numeric", month: "long", timeZone: "UTC" });
  const weekdayShort = new Intl.DateTimeFormat(dateLocale(locale), { weekday: "short", timeZone: "UTC" });
  const dayNumber = new Intl.DateTimeFormat(dateLocale(locale), { day: "numeric", timeZone: "UTC" });
  const asDate = (iso: string) => new Date(`${iso}T12:00:00Z`);
  const dateLine = weekView ? t("weekRange", { from: monthDay.format(asDate(days[0])), to: monthDay.format(asDate(days[6])) }) : longDay.format(asDate(view.date));

  const team = members ?? [];
  const nameOf = (m: MemberOut) => m.display_name ?? person("nameNotSet");
  const serviceOf = (b: AgendaOut) => serviceName(b.service_name as NameMap, locale as Locale, settings.language as Locale);

  const items = data
    ? mergeAgenda(data.bookings, data.blocks, tz, { from: range.from }, { role: isOwner ? "owner" : "worker", memberId: session.member_id })
    : [];

  // --- desktop columns: members (day) or the days of one person (week) ---
  const selfMember = view.member === "all" ? session.member_id : view.member;
  function cellsFor(list: Item[], day: string): Cell[] {
    const pairs = list.flatMap((item) => daySlices(item, [day], tz).map((slice) => ({ item, slice })));
    const bookings = pairs.filter((pair) => pair.item.kind === "booking");
    const packed = lanes(bookings.map((pair) => pair.slice));
    const cells: Cell[] = [
      ...bookings.map((pair, i) => ({ ...pair, ...packed[i] })),
      ...pairs.filter((pair) => pair.item.kind === "block").map((pair) => ({ ...pair, lane: 0, of: 1 })),
    ];
    // DOM order is reading order: by start, a block before a booking at the same minute.
    return cells.sort((a, b) => a.slice.start - b.slice.start || (a.item.kind === "block" ? -1 : 1) - (b.item.kind === "block" ? -1 : 1));
  }
  const dayMembers: { id: string; name: string }[] = !isOwner
    ? [{ id: session.member_id, name: session.display_name ?? person("nameNotSet") }]
    : (view.member === "all" ? team : team.filter((m) => m.member_id === view.member)).map((m) => ({ id: m.member_id, name: nameOf(m) }));
  const columns: Column[] = weekView
    ? days.map((day) => ({
        key: day,
        day,
        label: weekdayShort.format(asDate(day)),
        small: dayNumber.format(asDate(day)),
        ariaLabel: longDay.format(asDate(day)),
        cells: cellsFor(items.filter((item) => memberOf(item) === (isOwner ? selfMember : session.member_id)), day),
      }))
    : dayMembers.map((m) => ({ key: m.id, day: view.date, label: m.name, ariaLabel: m.name, cells: cellsFor(items.filter((item) => memberOf(item) === m.id), view.date) }));
  const everyCell = columns.flatMap((column) => column.cells);
  const hours = hourRange(
    data?.opening ?? [],
    days.map(weekdayOf),
    everyCell.map((cell) => ({ start: cell.slice.start, end: cell.slice.end, allDay: cell.item.kind === "block" && cell.item.allDay })),
  );
  const closedToday = !weekView && closedDay(data?.opening ?? [], weekdayOf(view.date));

  const top = (minutes: number) => `calc(var(--hour) * ${(minutes - hours.from * 60) / 60})`;

  function blockText(item: Extract<Item, { kind: "block" }>) {
    const reason = item.block.reason;
    return item.allDay
      ? { head: t("timeOff"), label: reason ? t("timeOffReason", { reason }) : t("timeOff") }
      : { head: t("blocked"), label: reason ? t("blockedReason", { reason }) : t("blocked") };
  }

  function renderCell(cell: Cell, column: Column) {
    const { item, slice } = cell;
    const start = Math.max(slice.start, hours.from * 60);
    const end = Math.min(slice.end, hours.to * 60);
    if (end <= start) return null;
    const place: CSSProperties = { top: top(start), height: `calc(var(--hour) * ${(end - start) / 60})` };
    if (item.kind === "block") {
      const text = blockText(item);
      const reason = item.block.reason;
      const full = item.allDay
        ? t("blockAllDay", { label: text.label })
        : t("blockTimes", { label: text.label, start: hhmm(slice.start), end: hhmm(slice.end) });
      return (
        <li key={`${item.key}-${column.key}`} className={css.block} style={place}>
          <span className={uiStyles.srOnly}>{full}</span>
          <span aria-hidden="true">
            <b>{text.head}</b>
            {reason}
          </span>
        </li>
      );
    }
    const { booking } = item;
    const chip = chipOf(booking.status);
    const words = [
      booking.status === "pending" ? t("wordWaiting") : booking.status === "no_show" ? t("wordNoShow") : null,
      booking.source === "merchant" ? today("walkIn") : null,
    ].filter(Boolean);
    const short = slice.end - slice.start < 30;
    const selected = booking.id === view.booking;
    const name = t("itemLabel", {
      start: localTime(booking.starts_at, tz),
      end: localTime(booking.ends_at, tz),
      client: booking.client_name,
      service: serviceOf(booking),
      status: chip ? today(chip.word) : booking.status,
    });
    const width: CSSProperties = { left: `calc(100% * ${cell.lane} / ${cell.of} + 2px)`, width: `calc(100% / ${cell.of} - 4px)` };
    const tone = booking.status === "pending" ? css.waiting : booking.status === "completed" || booking.status === "no_show" ? css.done : "";
    const icon = chip && booking.status !== "confirmed" && (
      <span className={css.ico} aria-hidden="true">
        {chip.icon}{" "}
      </span>
    );
    return (
      <li key={`${item.key}-${column.key}`} className={`${css.item} ${short ? css.short : ""} ${tone} ${selected ? css.selected : ""}`} style={{ ...place, ...width }}>
        <Link
          href={href({ booking: booking.id })}
          scroll={false}
          replace={Boolean(view.booking)}
          aria-label={name}
          aria-current={selected ? "true" : undefined}
          data-open={booking.id}
          onClick={() => {
            if (!view.booking) openedInApp.current = true;
          }}
        >
          <b>
            {icon}
            {booking.client_name}
          </b>
          {short ? (
            words.length > 0 && <span>{` · ${words.join(" · ")}`}</span>
          ) : (
            <span>{weekView ? null : [serviceOf(booking), ...words].join(" · ")}</span>
          )}
        </Link>
      </li>
    );
  }

  // --- phone list ---
  function dayList(day: string) {
    const list = items.filter((item) => (view.member === "all" || memberOf(item) === view.member) && daySlices(item, [day], tz).length > 0);
    const closed = closedDay(data?.opening ?? [], weekdayOf(day));
    if (list.length === 0) {
      return closed ? (
        <p className={css.noneToday}>
          <strong>{t("closedTitle")}</strong>
          {t("closedBody")}
        </p>
      ) : (
        <p className={css.noneToday}>{t("nothingBooked")}</p>
      );
    }
    const span = dayWindow(day, tz, 1);
    const isToday = day === todayISO;
    const line = isToday ? nowLineAt(list, now.getTime()) : -1;
    const nowItem = (k: string) => (
      <li key={k} className={styles.nowLine}>
        <span>{today("nowLine", { time: localTime(now.toISOString(), tz) })}</span>
      </li>
    );
    return (
      <ol className={styles.agenda}>
        {list.map((item, index) => {
          const past = isToday && agendaPhase(item, now.getTime()) === "past" ? styles.agendaPast : "";
          const marker = index === line && nowItem(`now-${item.key}`);
          if (item.kind === "block") {
            const text = blockText(item);
            const when = blockLabel(item, span, now, tz);
            return [
              marker,
              <li key={item.key} className={`${styles.agendaBlock} ${past}`}>
                <div className={styles.agendaTime}>
                  {when.kind === "allDay" ? today("allDay") : when.kind === "until" ? today("until", { time: when.time }) : when.time}
                  {when.kind === "timed" && <small>{today("minutes", { n: when.minutes })}</small>}
                </div>
                <div className={styles.agendaWhat}>
                  {text.label}
                  {isOwner && item.block.member_name && <span>{item.block.member_name}</span>}
                </div>
                <span />
              </li>,
            ];
          }
          const { booking } = item;
          const chip = chipOf(booking.status);
          const meta = [isOwner ? (booking.worker_display_name ?? person("nameNotSet")) : null, booking.source === "merchant" ? today("walkIn") : null].filter(Boolean);
          return [
            marker,
            <li key={item.key} className={past}>
              <Link
                className={css.rowLink}
                href={href({ booking: booking.id })}
                scroll={false}
                replace={Boolean(view.booking)}
                data-open={booking.id}
                onClick={() => {
                  if (!view.booking) openedInApp.current = true;
                }}
              >
                <div className={styles.agendaTime}>
                  {localTime(booking.starts_at, tz)}
                  <small>{today("minutes", { n: Math.round((item.end - item.start) / 60_000) })}</small>
                </div>
                <div className={styles.agendaWhat}>
                  {booking.client_name}
                  <span>{[serviceOf(booking), ...meta].join(" · ")}</span>
                </div>
                {chip ? (
                  <span className={`${styles.chip} ${chip.style}`}>
                    <span aria-hidden="true">{chip.icon}</span>
                    <span className={styles.chipWord}>{today(chip.word)}</span>
                  </span>
                ) : (
                  <span />
                )}
              </Link>
            </li>,
          ];
        })}
        {line === list.length && nowItem("now-end")}
      </ol>
    );
  }

  function onClose(event: MouseEvent<HTMLAnchorElement>) {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
    // Opened from this page: go back to where the person was. A deep link has no such place: replace.
    if (openedInApp.current) {
      event.preventDefault();
      openedInApp.current = false;
      router.back();
    }
  }

  const step = weekView ? 7 : 1;
  const colMember = weekView ? selfMember : view.member;
  const options = [...(weekView ? [] : [{ id: "all", name: t("everyone") }]), ...team.map((m) => ({ id: m.member_id, name: nameOf(m) }))];
  const empty = data !== null && everyCell.length === 0;

  return (
    <div className={`${css.page} ${view.booking ? css.hasDetail : ""}`}>
      <Heading focus>{nav("calendar")}</Heading>
      <div className={`${css.toolbar} ${css.hideWithDetail}`}>
        <p className={css.dateLabel} aria-live="polite">
          {dateLine}
        </p>
        <span className={css.stepper}>
          <Link href={href({ date: addDaysISO(view.date, -step) })} scroll={false} aria-label={weekView ? t("prevWeek") : t("prevDay")}>
            <span aria-hidden="true">{"‹"}</span>
          </Link>
          <Link href={href({ date: todayISO })} scroll={false}>
            {weekView ? t("thisWeek") : t("today")}
          </Link>
          <Link href={href({ date: addDaysISO(view.date, step) })} scroll={false} aria-label={weekView ? t("nextWeek") : t("nextDay")}>
            <span aria-hidden="true">{"›"}</span>
          </Link>
        </span>
        <nav className={css.seg} aria-label={t("view")}>
          <Link href={href({ view: "day" })} scroll={false} aria-current={weekView ? undefined : "true"}>
            {t("day")}
          </Link>
          <Link href={href({ view: "week" })} scroll={false} aria-current={weekView ? "true" : undefined}>
            {t("week")}
          </Link>
        </nav>
        {isOwner && members && (
          <div className={`${uiStyles.input} ${css.member} ${css.deskOnly}`}>
            <select aria-label={t("member")} value={colMember} onChange={(event) => router.push(href({ member: event.target.value }), { scroll: false })}>
              {options.map((option) => (
                <option key={option.id} value={option.id}>
                  {option.name}
                </option>
              ))}
            </select>
          </div>
        )}
      </div>
      {isOwner && members && (
        <nav className={`${css.chips} ${css.phoneOnly} ${css.hideWithDetail}`} aria-label={t("filter")}>
          {[{ id: "all", name: t("everyone") }, ...team.map((m) => ({ id: m.member_id, name: nameOf(m) }))].map((option) => (
            <Link key={option.id} href={href({ member: option.id })} scroll={false} aria-current={view.member === option.id ? "true" : undefined}>
              {option.name}
            </Link>
          ))}
        </nav>
      )}

      <div className={`${css.body} ${view.booking ? css.hasPanel : ""}`}>
        <div>
          {failed ? (
            <LoadFailure
              failure={failed.problem}
              onRetry={() => {
                setFailure(null);
                setReload((n) => n + 1);
              }}
            />
          ) : data === null ? (
            <>
              <div className={`${css.phoneOnly} ${css.hideWithDetail}`}>
                <Skeleton />
              </div>
              <div className={css.deskOnly} aria-busy="true">
                <p className={uiStyles.srOnly} role="status">
                  {today("loading")}
                </p>
                <div className={styles.skeleton} style={{ height: "28rem" }} />
              </div>
            </>
          ) : (
            <>
              <div className={`${css.phoneOnly} ${css.phoneList} ${css.hideWithDetail}`}>
                {weekView ? (
                  <div className={uiStyles.stack}>
                    {days.map((day) => (
                      <section key={day} className={css.dayBlock} aria-label={longDay.format(asDate(day))}>
                        <h2 className={css.dayHeading}>{longDay.format(asDate(day))}</h2>
                        {dayList(day)}
                      </section>
                    ))}
                  </div>
                ) : (
                  dayList(view.date)
                )}
              </div>
              <div className={`${css.deskOnly} ${uiStyles.stack}`}>
                {(closedToday || empty) && (
                  <p className={css.gridNote}>
                    <strong>{closedToday ? t("closedTitle") : t("nothingBooked")}</strong>
                    {closedToday && t("closedBody")}
                  </p>
                )}
                <div className={css.gridWrap} style={{ "--hours": hours.to - hours.from } as CSSProperties}>
                  <div>
                    <div className={css.gutterHead} />
                    <div className={css.hours} aria-hidden="true">
                      {Array.from({ length: hours.to - hours.from }, (_, i) => (
                        <span key={i} style={{ "--at": i } as CSSProperties}>
                          {`${hours.from + i}:00`}
                        </span>
                      ))}
                    </div>
                  </div>
                  <div className={css.scroller}>
                    <div className={css.inner} style={{ "--cols": columns.length, "--mincol": weekView ? "0px" : "8rem" } as CSSProperties}>
                      <div className={css.heads}>
                        {columns.map((column) => (
                          <div key={column.key} className={css.colHead} aria-current={weekView && column.day === todayISO ? "date" : undefined}>
                            {column.label}
                            {column.small && <small>{column.small}</small>}
                          </div>
                        ))}
                      </div>
                      <div className={css.lanes}>
                        {columns.map((column) => {
                          const line = nowTop(now, [column.day], hours, tz);
                          return (
                            <div key={column.key} className={`${css.lane} ${closedDay(data.opening, weekdayOf(column.day)) ? css.closed : ""}`}>
                              <ul className={css.laneList} role="list" aria-label={column.ariaLabel}>
                                {column.cells.map((cell) => renderCell(cell, column))}
                              </ul>
                              {line !== null && <div className={css.nowMark} style={{ top: `calc(var(--hour) * ${line / 60})` }} aria-hidden="true" />}
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  </div>
                </div>
                <p className={css.legend}>{t("legend")}</p>
              </div>
            </>
          )}
        </div>
        {view.booking && (
          <BookingDetail
            key={view.booking}
            id={view.booking}
            now={now}
            closeHref={href({ booking: null })}
            onClose={onClose}
            onChanged={() => setReload((n) => n + 1)}
          />
        )}
      </div>
    </div>
  );
}
