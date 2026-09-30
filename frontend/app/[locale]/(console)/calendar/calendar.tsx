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
import { closedDay, closedDetail, daySlices, dayWindow, hourRange, hrefFor, type CalendarView, lanes, nowTop, type Panel, parseView, type Slice, spanTimes, visibleDays, weekdayOf } from "@/lib/calendar";
import { dateLocale } from "@/lib/console";
import { type Locale, type NameMap, serviceName } from "@/lib/services";
import { hhmm, messageShown, slotAt } from "@/lib/new-booking";
import { addDaysISO, localDateISO, localTime, localToInstant } from "@/lib/time-off";
import { agendaPhase, blockLabel, mergeAgenda, nowLineAt } from "@/lib/today";

import { useConsole } from "../../_ui/console";
import styles from "../../_ui/console.module.css";
import { Heading, problem } from "../../_ui/parts";
import uiStyles from "../../_ui/ui.module.css";
import { chipOf, LoadFailure, Skeleton } from "../today";
import { BlockPanel } from "./block-panel";
import { BookingDetail } from "./booking-detail";
import { BookingForm, type Done } from "./booking-form";
import css from "./calendar.module.css";

type Loaded = { key: string; bookings: AgendaOut[]; blocks: TimeOffRangeOut[]; opening: Shift[] };
type Item = ReturnType<typeof mergeAgenda<AgendaOut, TimeOffRangeOut>>[number];
type Cell = { item: Item; slice: Slice; lane: number; of: number };
type Column = { key: string; day: string; label: string; ariaLabel: string; small?: string; cells: Cell[] };
/** The empty-spot popover: where it sits on screen, and the start and person the click meant. */
type Popover = { at: string; time: string; member: string; name: string; column: string; left: number; top: number };

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
  const panelOpenedInApp = useRef(false);
  const lastView = useRef<CalendarView | null>(null);
  const lastPanel = useRef<Panel | null>(null);
  // What opened the panel, to give focus back to when it closes.
  const invoker = useRef<HTMLElement | null>(null);
  // A booking just made or moved: focus goes to its grid item once the window has been read again.
  const justDone = useRef<{ id: string; before: Loaded | null } | null>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const [popover, setPopover] = useState<Popover | null>(null);
  // The hint under the heading: where it was made (booking, day, view), so it never lingers elsewhere.
  const [message, setMessage] = useState<{ text: string; booking: string | null; date: string; view: string } | null>(null);
  // What the screen reader hears: cleared before each set, so an identical message is announced again.
  const [live, setLive] = useState("");
  const liveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [settled, setSettled] = useState<string | null>(null);
  useEffect(() => () => {
    if (liveTimer.current) clearTimeout(liveTimer.current);
  }, []);
  function announce(text: string) {
    if (liveTimer.current) clearTimeout(liveTimer.current);
    setLive("");
    liveTimer.current = setTimeout(() => setLive(text), 50);
  }

  const view = parseView(params, todayISO, isOwner ? "owner" : "worker", session.member_id, members?.map((m) => m.member_id) ?? null);
  // Skipping the heading's focus is for the booking just made or moved, once: any other booking (or none)
  // in between clears it, so a reopened one focuses its heading again.
  const [seenBooking, setSeenBooking] = useState(view.booking);
  if (seenBooking !== view.booking) {
    setSeenBooking(view.booking);
    if (settled !== view.booking) setSettled(null);
  }
  // Opening a panel (reschedule, new, block) from the detail also ends that one-shot skip, so the detail
  // focuses its heading again when the panel closes.
  const [seenPanel, setSeenPanel] = useState(view.panel);
  if (seenPanel !== view.panel) {
    setSeenPanel(view.panel);
    if (view.panel) setSettled(null);
  }
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
  // A panel replacing the open booking is not a close: the panel has its own focus. Moving the date, view
  // or member drops the booking too, but that is not a close either: focus stays put.
  useEffect(() => {
    const before = lastView.current;
    lastView.current = view;
    if (!before?.booking || view.panel || !closedDetail(before, view)) return;
    const link = [...document.querySelectorAll<HTMLElement>(`[data-open="${CSS.escape(before.booking)}"]`)].find((el) => el.offsetParent !== null);
    (link ?? document.querySelector<HTMLElement>("h1"))?.focus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view.booking, view.panel, view.date, view.view, view.member]);

  // Closing a new-booking or block panel returns focus to what opened it (a move returns to the detail,
  // which takes focus itself). After a success the window's own focus effect below takes over.
  useEffect(() => {
    const before = lastPanel.current;
    lastPanel.current = view.panel;
    if (view.panel) return;
    panelOpenedInApp.current = false;
    if (!before || before === "move" || justDone.current) return;
    const el = invoker.current;
    invoker.current = null;
    (el?.isConnected ? el : document.querySelector<HTMLElement>("h1"))?.focus();
  }, [view.panel]);

  // The popover's own keyboard and pointer life: focus its first button, close on an outside press or a scroll.
  useEffect(() => {
    if (!popover) return;
    popoverRef.current?.querySelector<HTMLElement>("a")?.focus();
    const away = (event: Event) => {
      if (event.type === "pointerdown" && popoverRef.current?.contains(event.target as Node)) return;
      setPopover(null);
    };
    document.addEventListener("pointerdown", away);
    window.addEventListener("scroll", away, true);
    window.addEventListener("resize", away);
    return () => {
      document.removeEventListener("pointerdown", away);
      window.removeEventListener("scroll", away, true);
      window.removeEventListener("resize", away);
    };
  }, [popover]);

  const data = loaded && loaded.key === key ? loaded : null;
  const failed = failure && failure.key === key ? failure : null;

  // After a booking was made or moved and the window was read again: focus its grid item (the phone's
  // list is hidden behind the panel, so then its panel heading), else the page heading.
  useEffect(() => {
    const done = justDone.current;
    if (!done) return;
    // One shot, whether or not the window could be read again.
    if (failed) {
      justDone.current = null;
      return;
    }
    if (!data || data === done.before) return;
    justDone.current = null;
    const link = [...document.querySelectorAll<HTMLElement>(`[data-open="${CSS.escape(done.id)}"]`)].find((el) => el.offsetParent !== null);
    (link ?? document.getElementById("detail-title") ?? document.querySelector<HTMLElement>("h1"))?.focus();
  }, [data, failed]);
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
      const times = spanTimes(item, tz);
      const full = item.allDay ? t("blockAllDay", { label: text.label }) : t("blockTimes", { label: text.label, ...times });
      return (
        <li key={`${item.key}-${column.key}`} className={css.block} style={place}>
          <span className={uiStyles.srOnly}>{full}</span>
          <span aria-hidden="true">
            {weekView ? <b>{reason || text.head}</b> : <><b>{text.head}</b>{reason}</>}
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
    const short = (item.end - item.start) / 60_000 < 45; // one line: a 30-minute item is about 32px tall
    const second = weekView ? (short ? words : []) : [serviceOf(booking), ...words];
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
            second.length > 0 && <span>{` · ${second.join(" · ")}`}</span>
          ) : (
            <span>{second.join(" · ")}</span>
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

  // A click on a lane's empty area (never on an item) offers a booking or blocked time at that spot. The
  // keyboard route to the same actions is the toolbar's two buttons: a tab stop per empty slot would be dozens.
  function openSpot(event: MouseEvent<HTMLDivElement>, column: Column) {
    if (event.target !== event.currentTarget) return;
    const lane = event.currentTarget;
    const hour = parseFloat(getComputedStyle(lane).getPropertyValue("--hour")) || 64;
    const offset = ((event.clientY - lane.getBoundingClientRect().top) / hour) * 60;
    const time = slotAt(offset, hours.from, settings.slot_step_minutes, hours.to);
    const member = weekView ? selfMember : column.key;
    const chosen = team.find((m) => m.member_id === member);
    const name = chosen ? nameOf(chosen) : (session.display_name ?? person("nameNotSet"));
    setPopover({
      at: localToInstant(column.day, time, tz),
      time,
      member,
      name,
      column: column.key,
      left: Math.max(8, Math.min(event.clientX, window.innerWidth - 232)),
      top: Math.max(8, Math.min(event.clientY, window.innerHeight - 176)),
    });
  }

  function openPanel(event: MouseEvent<HTMLElement>) {
    invoker.current = event.currentTarget;
    // A panel already open (a deep link from Today) has no calendar entry to go back to.
    if (!view.panel) panelOpenedInApp.current = true;
  }

  function fromPopover(pop: Popover) {
    invoker.current = document.querySelector<HTMLElement>(`[data-col="${CSS.escape(pop.column)}"]`);
    if (!view.panel) panelOpenedInApp.current = true;
    setPopover(null);
  }

  function closePanel(event: MouseEvent<HTMLAnchorElement>) {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
    // Opened from this page: go back to where the person was. A deep link (Today's button) has no such place: replace.
    if (panelOpenedInApp.current) {
      event.preventDefault();
      panelOpenedInApp.current = false;
      router.back();
    }
  }

  function cancelPanel() {
    if (panelOpenedInApp.current) {
      panelOpenedInApp.current = false;
      router.back();
    } else {
      router.replace(href({ panel: null }), { scroll: false });
    }
  }

  function blocked() {
    setMessage({ text: t("blockedDone"), booking: null, date: view.date, view: view.view });
    announce(t("blockedDone"));
    setReload((n) => n + 1);
    panelOpenedInApp.current = false;
    router.replace(href({ panel: null }), { scroll: false });
  }

  function finished(done: Done) {
    justDone.current = { id: done.id, before: loaded };
    setSettled(done.id);
    openedInApp.current = false;
    const date = localDateISO(new Date(done.startsAt), tz);
    setMessage({ text: done.message, booking: done.id, date, view: view.view });
    announce(done.message);
    setReload((n) => n + 1);
    panelOpenedInApp.current = false;
    router.replace(href({ panel: null, booking: done.id, date, at: null, with: null }), { scroll: false });
  }

  const step = weekView ? 7 : 1;
  const colMember = weekView ? selfMember : view.member;
  const options = [...(weekView ? [] : [{ id: "all", name: t("everyone") }]), ...team.map((m) => ({ id: m.member_id, name: nameOf(m) }))];
  const empty = data !== null && everyCell.length === 0;

  return (
    <div className={`${css.page} ${view.booking || view.panel ? css.hasDetail : ""}`}>
      <Heading focus>{nav("calendar")}</Heading>
      <p className={uiStyles.srOnly} role="status" aria-live="polite">
        {live}
      </p>
      {message && messageShown(message, view) && (
        <p className={uiStyles.hint} aria-hidden="true">
          {message.text}
        </p>
      )}
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
        <span className={css.newActions}>
          <Link className={`${uiStyles.button} ${uiStyles.primary} ${uiStyles.small}`} href={href({ panel: "new", at: null, with: null })} scroll={false} replace={Boolean(view.panel)} onClick={openPanel}>
            {t("newBooking")}
          </Link>
          <Link className={`${uiStyles.button} ${uiStyles.secondary} ${uiStyles.small}`} href={href({ panel: "block", at: null, with: null })} scroll={false} replace={Boolean(view.panel)} onClick={openPanel}>
            {t("blockTime")}
          </Link>
        </span>
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

      <div className={`${css.body} ${view.booking || view.panel ? css.hasPanel : ""}`}>
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
                <div className={css.phoneActions}>
                  <Link className={`${uiStyles.button} ${uiStyles.primary} ${uiStyles.small}`} href={href({ panel: "new", at: null, with: null })} scroll={false} onClick={openPanel}>
                    {t("newBooking")}
                  </Link>
                  <Link className={`${uiStyles.button} ${uiStyles.secondary} ${uiStyles.small}`} href={href({ panel: "block", at: null, with: null })} scroll={false} onClick={openPanel}>
                    {t("blockTime")}
                  </Link>
                </div>
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
                          <div key={column.key} className={css.colHead} data-col={column.key} tabIndex={-1} aria-current={weekView && column.day === todayISO ? "date" : undefined}>
                            {column.label}
                            {column.small && <small>{column.small}</small>}
                          </div>
                        ))}
                      </div>
                      <div className={css.lanes}>
                        {columns.map((column) => {
                          const line = nowTop(now, [column.day], hours, tz);
                          return (
                            <div key={column.key} className={`${css.lane} ${closedDay(data.opening, weekdayOf(column.day)) ? css.closed : ""}`} onClick={(event) => openSpot(event, column)}>
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
        {view.panel && (!isOwner || members) ? (
          view.panel === "block" ? (
            <BlockPanel
              key={`${view.at}|${view.with}`}
              at={view.at}
              member={view.with}
              team={team}
              date={view.date}
              closeHref={href({ panel: null })}
              onClose={closePanel}
              onCancel={cancelPanel}
              onDone={blocked}
            />
          ) : (
            <BookingForm
              key={`${view.panel}|${view.booking}|${view.at}|${view.with}`}
              mode={view.panel}
              bookingId={view.booking}
              prefill={{ at: view.at, with: view.with }}
              team={team}
              todayISO={todayISO}
              date={view.date}
              closeHref={href({ panel: null })}
              onClose={closePanel}
              onDone={finished}
            />
          )
        ) : view.panel ? (
          <aside className={css.panel} aria-busy="true">
            <Skeleton />
          </aside>
        ) : (
          view.booking && (
            <BookingDetail
              key={view.booking}
              id={view.booking}
              now={now}
              closeHref={href({ booking: null })}
              onClose={onClose}
              onChanged={() => setReload((n) => n + 1)}
              moveHref={href({ panel: "move" })}
              onMove={() => {
                panelOpenedInApp.current = true;
              }}
              focusOnOpen={settled !== view.booking}
            />
          )
        )}
      </div>
      {popover && (
        <div
          ref={popoverRef}
          role="group"
          aria-label={t("popoverGroup", { member: popover.name, time: popover.time })}
          className={css.popover}
          style={{ left: popover.left, top: popover.top }}
          onBlur={(event) => {
            // Tab out of the popover closes it (a null target is a click or the window losing focus).
            if (event.relatedTarget && !event.currentTarget.contains(event.relatedTarget as Node)) setPopover(null);
          }}
          onKeyDown={(event) => {
            if (event.key !== "Escape") return;
            event.stopPropagation();
            const column = document.querySelector<HTMLElement>(`[data-col="${CSS.escape(popover.column)}"]`);
            setPopover(null);
            column?.focus();
          }}
        >
          <small aria-hidden="true">{`${popover.name} · ${popover.time}`}</small>
          <Link href={href({ panel: "new", at: popover.at, with: popover.member })} scroll={false} replace={Boolean(view.panel)} onClick={() => fromPopover(popover)}>
            {t("popoverNew")}
          </Link>
          <Link href={href({ panel: "block", at: popover.at, with: popover.member })} scroll={false} replace={Boolean(view.panel)} onClick={() => fromPopover(popover)}>
            {t("popoverBlock")}
          </Link>
        </div>
      )}
    </div>
  );
}
