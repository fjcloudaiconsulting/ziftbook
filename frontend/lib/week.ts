// Pure logic shared by the opening-hours (PR 2) and working-hours (PR 4) week editors: turning the
// UI's per-day shift lists into the PUT body, checking them for problems before a save, and the
// weekday/city/list formatting the savebar and error banners use. Kept free of React so
// node --test can run it directly.

import { dateLocale } from "./console.ts";

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
 */
export function weekProblems(days: Day[], envelope: Day[] | null): WeekProblems {
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
  if (!anyOpen) return { byDay, overall: "opening_hours_required" };
  return { byDay };
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
 * Wednesday and Friday", `dateLocale`: en-GB drops the Oxford comma).
 */
export function daysSummary(weekdays: number[], locale: string): string {
  const sorted = [...weekdays].sort((a, b) => a - b);
  const dl = dateLocale(locale);
  const isRun = sorted.length >= 3 && sorted.every((w, i) => i === 0 || w === sorted[i - 1] + 1);
  if (isRun) return `${weekdayName(sorted[0], dl)} to ${weekdayName(sorted.at(-1)!, dl)}`;
  return new Intl.ListFormat(dl, { type: "conjunction" }).format(sorted.map((w) => weekdayName(w, dl)));
}
