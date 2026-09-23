// Pure logic shared by the opening-hours and working-hours week editors: turning the UI's per-day
// shift lists into the PUT body, checking them for problems before a save, and the
// weekday/city/list formatting the savebar and error banners use. Kept free of React (and of any
// sibling import) so node --test can run it directly.

export const WEEKDAYS = [1, 2, 3, 4, 5, 6, 7] as const;

export type Shift = { start: string; end: string };
export type Day = { weekday: number; shifts: Shift[] };

/** Seven closed days, for the empty state's "Set the opening week" button. */
export function emptyWeek(): Day[] {
  return WEEKDAYS.map((weekday) => ({ weekday, shifts: [] }));
}

/** The GET response turned into all seven weekdays (closed ones included), each day's shifts
 * sorted by start. */
export function daysFromShifts(shifts: { weekday: number; starts_at: string; ends_at: string }[]): Day[] {
  return WEEKDAYS.map((weekday) => ({
    weekday,
    shifts: shifts
      .filter((s) => s.weekday === weekday)
      .map((s) => ({ start: s.starts_at, end: s.ends_at }))
      .sort((a, b) => (a.start < b.start ? -1 : a.start > b.start ? 1 : 0)),
  }));
}

/** An opening-hours GET turned into a `weekProblems` envelope. `[]` means never configured
 * (`schedule.py:170-171`), which must stay `null` (unbounded) here: seven empty `daysFromShifts`
 * days would instead read as "closed every day", refusing every shift. */
export function envelopeFromShifts(shifts: { weekday: number; starts_at: string; ends_at: string }[]): Day[] | null {
  return shifts.length === 0 ? null : daysFromShifts(shifts);
}

/** The PUT body: every non-empty shift, flattened and sorted by weekday then start (never by
 * JS `Date#getDay()`, which is 0-indexed and puts Sunday first). */
export function weekBody(days: Day[]): { weekday: number; starts_at: string; ends_at: string }[] {
  return days
    .flatMap((day) => day.shifts.filter((s) => s.start !== "" && s.end !== "").map((s) => ({ weekday: day.weekday, ...s })))
    .sort((a, b) => a.weekday - b.weekday || (a.start < b.start ? -1 : a.start > b.start ? 1 : 0))
    .map(({ weekday, start, end }) => ({ weekday, starts_at: start, ends_at: end }));
}

function sortShifts(shifts: Shift[]): Shift[] {
  return [...shifts].sort((a, b) => (a.start < b.start ? -1 : a.start > b.start ? 1 : 0));
}

/** Touching shifts join (09:00-12:00 + 12:00-15:00 becomes one 09:00-15:00 window), the same rule
 * the server applies to the opening envelope (`schedule.py:67-75`). */
function joinShifts(shifts: Shift[]): Shift[] {
  const joined: Shift[] = [];
  for (const shift of sortShifts(shifts)) {
    const last = joined.at(-1);
    if (last && shift.start === last.end) last.end = shift.end;
    else joined.push({ ...shift });
  }
  return joined;
}

export type WeekProblems = {
  byDay: Partial<Record<number, string>>;
  overall?: "opening_hours_required" | "too_many";
};

/**
 * Field and structural problems in a week, checked in the browser before every PUT so the same
 * codes the server would refuse with (`schedule.py:159-174,303-308`) are caught first.
 *
 * `envelope`: `null` means it was never configured, which bounds nothing (`schedule.py:170-171`).
 * A configured envelope (a non-empty list) that has no entry for a weekday treats that weekday as
 * closed, never as unbounded.
 *
 * `options.allowEmptyWeek`: opening hours refuses an all-closed week
 * (`opening_hours_required`, `schedule.py:304`); working hours does not (`schedule.py:139-190`
 * accepts an empty list, e.g. an owner clearing a leaving worker's week). Off by default, so every
 * existing opening-hours call keeps refusing it.
 */
export function weekProblems(days: Day[], envelope: Day[] | null, options?: { allowEmptyWeek?: boolean }): WeekProblems {
  const byDay: Partial<Record<number, string>> = {};
  const bounded = envelope !== null && envelope.length > 0;
  const envelopeByWeekday = new Map<number, Shift[]>((envelope ?? []).map((d) => [d.weekday, d.shifts]));
  let totalShifts = 0;
  let anyOpen = false;

  for (const day of days) {
    totalShifts += day.shifts.length;
    if (day.shifts.length === 0) continue;
    anyOpen = true;

    if (day.shifts.some((s) => s.start === "" || s.end === "")) {
      byDay[day.weekday] = "time_required";
      continue;
    }

    const sorted = sortShifts(day.shifts);

    if (sorted.some((s) => s.end <= s.start)) {
      byDay[day.weekday] = "end_not_after_start";
      continue;
    }

    const overlaps = sorted.some((shift, i) => i > 0 && shift.start < sorted[i - 1].end);
    if (overlaps) {
      byDay[day.weekday] = "overlapping_hours";
      continue;
    }

    if (bounded) {
      const windows = joinShifts(envelopeByWeekday.get(day.weekday) ?? []);
      const fits = sorted.every((shift) => windows.some((w) => w.start <= shift.start && shift.end <= w.end));
      if (!fits) {
        byDay[day.weekday] = "outside_opening_hours";
        continue;
      }
    }
  }

  if (totalShifts > 50) return { byDay, overall: "too_many" };
  if (!anyOpen && !options?.allowEmptyWeek) return { byDay, overall: "opening_hours_required" };
  return { byDay };
}

/**
 * The per-day problems worth flagging as soon as a week loads (spec PR 4, §5 item 6): a shift left
 * outside the envelope after the owner narrowed it, caught before the save the server would
 * refuse. Never `overall`: `too_many` and `opening_hours_required` are submit-time refusals of
 * what the person just did, not something to greet an untouched, freshly-loaded week with (an
 * empty opening-hours week loads via `emptyWeek()`, and must start idle, not in the error phase).
 */
export function loadTimeProblems(days: Day[], envelope: Day[] | null, options?: { allowEmptyWeek?: boolean }): WeekProblems {
  return { byDay: weekProblems(days, envelope, options).byDay };
}

/** Whether two days' shifts are the same set, ignoring order (reordering within a day is no
 * change). */
function sameShifts(a: Shift[], b: Shift[]): boolean {
  if (a.length !== b.length) return false;
  const key = (s: Shift) => `${s.start}-${s.end}`;
  const as = a.map(key).sort();
  const bs = b.map(key).sort();
  return as.every((v, i) => v === bs[i]);
}

/** The weekdays whose shifts differ between the loaded week and the current one, for the savebar's
 * dirty/error hints. */
export function changedDays(before: Day[], after: Day[]): number[] {
  const beforeByWeekday = new Map(before.map((d) => [d.weekday, d.shifts]));
  const afterByWeekday = new Map(after.map((d) => [d.weekday, d.shifts]));
  const weekdays = new Set([...beforeByWeekday.keys(), ...afterByWeekday.keys()]);
  return [...weekdays]
    .filter((w) => !sameShifts(beforeByWeekday.get(w) ?? [], afterByWeekday.get(w) ?? []))
    .sort((a, b) => a - b);
}

/** "Tuesday", built from a fixed Monday-first week (2024-01-01 is a Monday) so a weekday number
 * never goes through `Date#getDay()` (0-indexed, Sunday first) or the browser's local zone. */
export function weekdayName(n: number, locale: string): string {
  const date = new Date(Date.UTC(2024, 0, n));
  return new Intl.DateTimeFormat(locale, { weekday: "long", timeZone: "UTC" }).format(date);
}

/** The city name a time zone id ends with ("Europe/Amsterdam" -> "Amsterdam"). */
export function zoneCity(tz: string): string {
  return (tz.split("/").pop() ?? tz).replaceAll("_", " ");
}

/**
 * "a, b, and c": the drawn error banner's list, in the plain URL locale (Oxford comma), never
 * `dateLocale` (en-GB drops it).
 */
export function problemList(phrases: string[], locale: string): string {
  return new Intl.ListFormat(locale, { type: "conjunction" }).format(phrases);
}

/**
 * Weekdays as one run ("Tuesday to Saturday") when three or more sort into one unbroken sequence
 * that never wraps Sunday to Monday (the week is Monday-first); otherwise a plain list ("Monday,
 * Wednesday and Friday", no Oxford comma in en-GB). `locale` is already the caller's resolved
 * date locale (`dateLocale(locale)` from `lib/console.ts`): this file takes no sibling import, so
 * it stays runnable by `node --test` with no module resolution to configure. `formatRange` is the
 * catalog's `{from} to {to}` phrase, so the word between the two weekdays is translated too.
 */
export function daysSummary(weekdays: number[], locale: string, formatRange: (from: string, to: string) => string): string {
  const sorted = [...weekdays].sort((a, b) => a - b);
  const isRun = sorted.length >= 3 && sorted.every((w, i) => i === 0 || w === sorted[i - 1] + 1);
  if (isRun) return formatRange(weekdayName(sorted[0], locale), weekdayName(sorted.at(-1)!, locale));
  return new Intl.ListFormat(locale, { type: "conjunction" }).format(sorted.map((w) => weekdayName(w, locale)));
}

/** The envelope's shifts for one weekday, sorted, or `[]` when the shop doesn't open that day.
 * `envelope === null` (unbounded) has no per-day line at all; callers check that first. */
export function envelopeShiftsFor(envelope: Day[], weekday: number): Shift[] {
  return sortShifts(envelope.find((d) => d.weekday === weekday)?.shifts ?? []);
}

/** "09:00 – 18:00" or, for two blocks, "09:00 – 13:00 · 14:00 – 18:00": the working-hours day
 * head's "Shop open {ranges}" line. Only the punctuation is fixed here (an en dash and a
 * middot, the same locale-invariant separators the shift row and the services meta line already
 * use); every word around `{ranges}` comes from the catalog. */
export function shiftRanges(shifts: Shift[]): string {
  return shifts.map((s) => `${s.start} – ${s.end}`).join(" · ");
}

/**
 * "Copy to every day": the source weekday's shifts, applied to every day the shop is open (every
 * day, when the envelope is unbounded). A day the shop is closed keeps whatever it had (never
 * force-closed, and never given a shift the server would refuse).
 */
export function copyToEveryDay(days: Day[], envelope: Day[] | null, sourceWeekday: number): Day[] {
  const source = days.find((d) => d.weekday === sourceWeekday);
  if (!source) return days;
  return days.map((day) => {
    const shopOpen = envelope === null || envelopeShiftsFor(envelope, day.weekday).length > 0;
    return shopOpen ? { ...day, shifts: source.shifts.map((s) => ({ ...s })) } : day;
  });
}

/**
 * The team row's "Works {days}" meta, or `null` for "No working hours yet". Kept out of the
 * component so an empty week is never fed through `daysSummary` (an empty weekday list formats as
 * `""`, which a naive check would treat as truthy and show as "Works ").
 */
export function teamHoursSummary(
  shifts: { weekday: number }[],
  locale: string,
  formatRange: (from: string, to: string) => string,
): string | null {
  if (shifts.length === 0) return null;
  const weekdays = [...new Set(shifts.map((s) => s.weekday))];
  return daysSummary(weekdays, locale, formatRange);
}

/** The shift on a day whose start (or end) overlaps another, and the exact overlap window (the
 * intersection, not the outer span): two shifts 09:00-18:00 and 10:00-11:00 overlap 10:00-11:00,
 * never 10:00-18:00. Used to name the window in the field error under the day. */
export function overlapWindow(shifts: Shift[]): { from: string; to: string } | null {
  const sorted = sortShifts(shifts);
  for (let i = 1; i < sorted.length; i++) {
    if (sorted[i].start < sorted[i - 1].end) {
      return { from: sorted[i].start, to: sorted[i].end < sorted[i - 1].end ? sorted[i].end : sorted[i - 1].end };
    }
  }
  return null;
}

type SaveOutcome = { status: number; code?: string; weekday?: number; data?: unknown[] };

export type SaveResult =
  | { kind: "saved"; data: unknown[] }
  | { kind: "outsideOpeningHours"; weekday: number }
  | { kind: "serverProblem"; code: "opening_hours_required" | "end_not_after_start" | "overlapping_hours" }
  | { kind: "failure"; outcome: { status: number; code?: string } };

/**
 * Maps a save attempt's outcome to the one thing the editor does next, so every outcome - success,
 * every server refusal, and a request that never came back at all - resolves to a result the caller
 * can act on. `outcome` is `null` when the write itself threw (a rejected promise: a network drop,
 * a browser going offline mid-request), which is exactly the bug this fences: an un-caught throw
 * between `setPhase("saving")` and the next `setPhase` left the savebar showing a spinner forever,
 * because nothing after the throw ever ran. Routed through this function first, a threw write is
 * `{ kind: "failure" }` like any other unreachable server, never a case the caller has to remember
 * to handle separately.
 */
export function saveResult(outcome: SaveOutcome | null): SaveResult {
  if (outcome === null) return { kind: "failure", outcome: { status: 0 } };
  if (outcome.status === 200 && outcome.data) return { kind: "saved", data: outcome.data };
  if (outcome.status === 422 && outcome.code === "outside_opening_hours" && outcome.weekday) {
    return { kind: "outsideOpeningHours", weekday: outcome.weekday };
  }
  if (
    outcome.status === 422 &&
    (outcome.code === "opening_hours_required" || outcome.code === "end_not_after_start" || outcome.code === "overlapping_hours")
  ) {
    return { kind: "serverProblem", code: outcome.code };
  }
  return { kind: "failure", outcome: { status: outcome.status, code: outcome.code } };
}
