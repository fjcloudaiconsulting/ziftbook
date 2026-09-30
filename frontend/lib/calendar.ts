// Pure logic behind the calendar: Monday weeks, day windows that follow DST, where an item sits in a
// day column (wall-clock minutes of the business zone, never ms since midnight), lane packing, the
// visible hour range, which actions a booking offers, its history lines and the URL state. Free of
// React so node --test can run it in any runner zone.
import { addDaysISO, localDateISO, localTime, localToInstant } from "./time-off.ts";

export type Role = "owner" | "worker";
export type View = "day" | "week";

const MIN_MINUTES = 15; // the shortest an item is drawn (and packed): keeps a 5-minute block, or a fall-back hour that reads 02:15 to 02:15, visible

/** ISO weekday of a YYYY-MM-DD: Monday 1 .. Sunday 7. Plain UTC arithmetic, never the runner's zone. */
export function weekdayOf(dateISO: string): number {
  const [y, m, d] = dateISO.split("-").map(Number);
  return ((new Date(Date.UTC(y, m - 1, d)).getUTCDay() + 6) % 7) + 1;
}

/** The Monday on or before a date. ponytail: Monday-first for every shipped locale; switch to
 * Intl.Locale#getWeekInfo when a Sunday-first one ships. */
export function weekStart(dateISO: string): string {
  return addDaysISO(dateISO, 1 - weekdayOf(dateISO));
}

/** `n` local days from `dateISO`, as the instants the API takes: midnight to midnight in `tz`, so a
 * DST day is 23 or 25 hours rather than a fixed 24. */
export function dayWindow(dateISO: string, tz: string, n: number): { from: string; to: string } {
  return { from: localToInstant(dateISO, "00:00", tz), to: localToInstant(addDaysISO(dateISO, n), "00:00", tz) };
}

/** The local days a view shows: its seven days from Monday, or the one day. */
export function visibleDays(view: View, dateISO: string): string[] {
  const first = view === "week" ? weekStart(dateISO) : dateISO;
  return Array.from({ length: view === "week" ? 7 : 1 }, (_, i) => addDaysISO(first, i));
}

/** The booking panel's "When", in `tz`: whether the start is today, both clock times, and whether the
 * end falls on a later local day (so the line names its date). */
export function whenParts(startISO: string, endISO: string, now: Date, tz: string) {
  const day = localDateISO(new Date(startISO), tz);
  return {
    today: day === localDateISO(now, tz),
    startTime: localTime(startISO, tz),
    endTime: localTime(endISO, tz),
    endsLater: localDateISO(new Date(endISO), tz) !== day,
  };
}

export type Slice = { day: string; start: number; end: number };

function localMinutes(instant: number, tz: string): number {
  const [h, m] = localTime(new Date(instant).toISOString(), tz).split(":").map(Number);
  return h * 60 + m;
}

/** The part of an item on each of `days`, as wall-clock minutes in `tz` (0..1440) per day column: a
 * booking over midnight is one slice on each day, a block from yesterday starts at 0. Fall-back's
 * repeated hour maps both copies to the same rows. ponytail: that ceiling is accepted. */
export function daySlices(item: { start: number; end: number }, days: string[], tz: string): Slice[] {
  const slices: Slice[] = [];
  for (const day of days) {
    const dayStart = new Date(localToInstant(day, "00:00", tz)).getTime();
    const nextStart = new Date(localToInstant(addDaysISO(day, 1), "00:00", tz)).getTime();
    if (item.end <= dayStart || item.start >= nextStart) continue;
    const start = localMinutes(Math.max(item.start, dayStart), tz);
    const end = item.end >= nextStart ? 1440 : localMinutes(item.end, tz);
    slices.push({ day, start, end: Math.max(end, Math.min(1440, start + MIN_MINUTES)) });
  }
  return slices;
}

/** Side-by-side lanes for overlapping items in one column: each cluster of items that overlap (end ==
 * start does not) is split into as many lanes as it needs, and only that cluster narrows. Results are
 * in the input's order. */
export function lanes(slices: { start: number; end: number }[]): { lane: number; of: number }[] {
  const order = slices.map((_, i) => i).sort((a, b) => slices[a].start - slices[b].start || slices[a].end - slices[b].end);
  const out = new Array<{ lane: number; of: number }>(slices.length);
  let cluster: number[] = [];
  let clusterEnd = -1;
  let laneEnds: number[] = [];
  const close = () => {
    for (const i of cluster) out[i].of = laneEnds.length;
    cluster = [];
    laneEnds = [];
  };
  for (const i of order) {
    const { start, end } = slices[i];
    if (cluster.length > 0 && start >= clusterEnd) close();
    let lane = laneEnds.findIndex((laneEnd) => laneEnd <= start);
    if (lane === -1) lane = laneEnds.length;
    laneEnds[lane] = end;
    clusterEnd = cluster.length === 0 ? end : Math.max(clusterEnd, end);
    out[i] = { lane, of: 1 };
    cluster.push(i);
  }
  close();
  return out;
}

const toMinutes = (hhmm: string) => {
  const [h, m] = hhmm.split(":").map(Number);
  return h * 60 + m;
};

/** The hours the grid shows: the visible weekdays' opening hours rounded outwards (09-18 when none of
 * them has any, including a week never configured), widened by every timed item. A whole-day block is
 * not a time and never widens it. */
export function hourRange(
  shifts: { weekday: number; starts_at: string; ends_at: string }[],
  weekdays: number[],
  slices: { start: number; end: number; allDay?: boolean }[],
): { from: number; to: number } {
  const open = shifts.filter((shift) => weekdays.includes(shift.weekday));
  let from = open.length ? Math.floor(Math.min(...open.map((s) => toMinutes(s.starts_at))) / 60) : 9;
  let to = open.length ? Math.ceil(Math.max(...open.map((s) => toMinutes(s.ends_at))) / 60) : 18;
  for (const slice of slices) {
    if (slice.allDay) continue;
    from = Math.min(from, Math.floor(slice.start / 60));
    to = Math.max(to, Math.ceil(slice.end / 60));
  }
  return { from, to };
}

/** True only for a configured week without any time on this weekday. `[]` is a week nobody has set up
 * yet (`week.ts` envelopeFromShifts), not a closed one. */
export function closedDay(shifts: { weekday: number }[], weekday: number): boolean {
  return shifts.length > 0 && !shifts.some((shift) => shift.weekday === weekday);
}

/** Minutes from the top of the grid to the now line, or null when today is not one of `days` or now is
 * outside the shown hours. */
export function nowTop(now: Date, days: string[], range: { from: number; to: number }, tz: string): number | null {
  if (!days.includes(localDateISO(now, tz))) return null;
  const minutes = localMinutes(now.getTime(), tz);
  return minutes >= range.from * 60 && minutes < range.to * 60 ? minutes - range.from * 60 : null;
}

export type Action = "accept" | "decline" | "completed" | "no_show" | "restore" | "cancel" | "reschedule";

/** What a booking's panel offers, mirroring the API's TRANSITIONS and OWNER_ONLY_SOURCES (the API
 * decides; this only keeps buttons that would 409 off the screen). Completed and no-show wait for the
 * start; leaving `completed` is the owner's alone. */
export function actionsFor(detail: { status: string; expired: boolean; starts_at: string }, role: Role, now: Date): Action[] {
  const started = new Date(detail.starts_at).getTime() <= now.getTime();
  switch (detail.status) {
    case "pending":
      return detail.expired ? [] : ["accept", "decline"];
    case "confirmed":
      // Only a confirmed booking moves: a pending one is accepted first (the client's request was for that time).
      return started ? ["completed", "no_show", "reschedule", "cancel"] : ["reschedule", "cancel"];
    case "completed":
      return role === "owner" ? ["restore", "no_show", "cancel"] : [];
    default:
      return [];
  }
}

export type HistoryEvent = { event: string; at: string; actor?: string; actor_name: string | null; details: { [key: string]: unknown } | null };
export type HistoryKey =
  | "historyRequested"
  | "historyAdded"
  | "historyAccepted"
  | "historyUndone"
  | "historyDeclined"
  | "historyCancelled"
  | "historyCompleted"
  | "historyNoShow"
  | "historyCancelledByClient"
  | "historyConsent"
  | "historyExpired"
  | "historyMoved"
  | "historyMovedTeam"
  | "historyUpdated";

const BY_TEAM: Record<string, HistoryKey> = {
  declined: "historyDeclined",
  cancelled_by_merchant: "historyCancelled",
  completed: "historyCompleted",
  no_show: "historyNoShow",
};
// Events that change the status: the one before a `confirmed` tells an accept from an undo.
const STATUS_EVENTS = ["created", "confirmed", "declined", "cancelled_by_merchant", "cancelled_by_client", "completed", "no_show", "expired"];

/** One history line as a catalog key plus its parts; the caller words it (`actor` null = "a team
 * member") and formats the dates. Never throws: anything unknown reads "Updated". */
export function historyLabel(
  events: HistoryEvent[],
  i: number,
  source: string,
  maxReschedules: number,
  tz: string,
): { key: HistoryKey; params: Record<string, string | number | boolean | null> } {
  const e = events[i];
  const actor = e.actor_name;
  switch (e.event) {
    case "created":
      return { key: source === "merchant" ? "historyAdded" : "historyRequested", params: { actor } };
    case "confirmed": {
      const before = events.slice(0, i).reverse().find((x) => STATUS_EVENTS.includes(x.event));
      return { key: before?.event === "completed" ? "historyUndone" : "historyAccepted", params: { actor } };
    }
    case "cancelled_by_client":
      return { key: "historyCancelledByClient", params: {} };
    case "consent_confirmed":
      return { key: "historyConsent", params: {} };
    case "expired":
      return { key: "historyExpired", params: {} };
    case "rescheduled": {
      const from = e.details?.from;
      const to = e.details?.to;
      if (typeof from !== "string" || typeof to !== "string" || Number.isNaN(Date.parse(from)) || Number.isNaN(Date.parse(to))) {
        return { key: "historyUpdated", params: {} };
      }
      const fromDate = localDateISO(new Date(from), tz);
      const toDate = localDateISO(new Date(to), tz);
      const parts = { fromTime: localTime(from, tz), toTime: localTime(to, tz), fromDate, toDate, sameDay: fromDate === toDate };
      // The business moving a booking does not spend the client's changes: no "k of max", and k counts theirs only.
      if (e.actor === "team") return { key: "historyMovedTeam", params: { actor, ...parts } };
      const k = events.slice(0, i + 1).filter((x) => x.event === "rescheduled" && x.actor !== "team").length;
      return { key: "historyMoved", params: { ...parts, k, max: maxReschedules } };
    }
    default:
      return { key: BY_TEAM[e.event] ?? "historyUpdated", params: BY_TEAM[e.event] ? { actor } : {} };
  }
}

export type Panel = "new" | "block" | "move";
export type CalendarView = { view: View; date: string; member: string; booking: string | null; panel: Panel | null; at: string | null; with: string | null };

function validDate(value: string | null): value is string {
  return value !== null && /^\d{4}-\d{2}-\d{2}$/.test(value) && addDaysISO(value, 0) === value;
}

/** An instant the URL carries, as canonical UTC ISO, or null: never a string that would throw later. */
function validInstant(value: string | null): string | null {
  if (!value || !/^\d{4}-\d{2}-\d{2}T/.test(value) || Number.isNaN(Date.parse(value))) return null;
  return new Date(value).toISOString();
}

/** The URL's state, never trusted: a bad date is today, an unknown view is the day, a worker is always
 * themselves, an owner's member is `all` or an id (an id that is not one of `memberIds` once those are
 * known is `all`). A panel is `new`, `block` or `move` (which needs a booking); `new` and `block` replace
 * the open booking and carry `at` (an instant) and `with` (a member); a worker's `with` is themselves. */
export function parseView(params: { get(name: string): string | null }, todayISO: string, role: Role, selfId: string, memberIds?: string[] | null): CalendarView {
  const date = params.get("date");
  const asked = params.get("member");
  let member = role === "worker" ? selfId : (asked ?? "all");
  if (role === "owner" && member !== "all" && memberIds && !memberIds.includes(member)) member = "all";
  const wanted = params.get("panel");
  let booking = params.get("booking") || null;
  let panel: Panel | null = wanted === "new" || wanted === "block" || wanted === "move" ? wanted : null;
  if (panel === "move" && !booking) panel = null;
  if (panel === "new" || panel === "block") booking = null;
  const prefill = panel === "new" || panel === "block";
  let withMember: string | null = null;
  if (prefill) {
    const asWith = params.get("with");
    withMember = role === "worker" ? selfId : asWith && (!memberIds || memberIds.includes(asWith)) ? asWith : null;
  }
  return {
    view: params.get("view") === "week" ? "week" : "day",
    date: validDate(date) ? date : todayISO,
    member,
    booking,
    panel,
    at: prefill ? validInstant(params.get("at")) : null,
    with: withMember,
  };
}

/** A calendar URL (no locale: the locale-aware Link adds it). Whatever the patch does not name is
 * dropped when it moves the window (date, view or member): an open booking or panel never outlives what
 * it was opened from. `new` and `block` replace the open booking; `move` needs one. */
export function hrefFor(current: Partial<CalendarView> & Pick<CalendarView, "view" | "date" | "member">, patch: Partial<CalendarView>): string {
  const next = { ...current, ...patch };
  const moved = next.date !== current.date || next.view !== current.view || next.member !== current.member;
  const carry = (name: "booking" | "panel" | "at" | "with") => (moved && !(name in patch) ? null : (next[name] ?? null));
  let booking = carry("booking");
  let panel = carry("panel");
  if (panel === "new" || panel === "block") booking = null;
  if (panel === "move" && !booking) panel = null;
  const query = new URLSearchParams({ view: next.view, date: next.date, member: next.member });
  if (booking) query.set("booking", booking);
  if (panel) {
    query.set("panel", panel);
    const at = carry("at");
    const withMember = carry("with");
    if (at) query.set("at", at);
    if (withMember) query.set("with", withMember);
  }
  return `/calendar?${query}`;
}
