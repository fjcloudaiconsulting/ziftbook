// Pure logic for blocked time, including the list window. The server owns whole-day midnight (a
// request sends a bare `{first_day, last_day}` date pair, never computed here); this file only
// turns a partial block's local date + HH:MM (in the business time zone) into the UTC instant the
// API expects, DST included, and the other pure decisions the blocked-time screens need. Kept
// free of React (and of any sibling import) so node --test can run it directly.

/** The UTC offset (ms) `tz` is at a given instant, read from `Intl` rather than assumed, so a
 * DST transition is never guessed at. */
function offsetAt(utcMillis: number, tz: string): number {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: tz,
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).formatToParts(new Date(utcMillis));
  const map = Object.fromEntries(parts.map((p) => [p.type, p.value]));
  const asUtc = Date.UTC(
    Number(map.year),
    Number(map.month) - 1,
    Number(map.day),
    Number(map.hour) % 24,
    Number(map.minute),
    Number(map.second),
  );
  return asUtc - utcMillis;
}

// A day is ample distance from `local` to sample an offset guaranteed clear of whatever DST
// transition, if any, `local` itself sits inside or next to (real transitions are always more
// than a day apart), while still being close enough that only the ONE relevant transition (if
// any) can fall between the two samples.
const DAY_MS = 24 * 60 * 60 * 1000;

/**
 * A local wall-clock date + time in `tz`, turned into the instant the API takes (`starts_at` /
 * `ends_at`), matching the server's own rule exactly (backend `schedule.to_utc`): a wall-clock
 * time the change SKIPS (spring-forward) uses the offset from before the change; a wall-clock
 * time the change repeats (fall-back) resolves to its FIRST occurrence, i.e. also the offset from
 * before the change. Never `new Date(\`${date}T${time}\`)` (the runner's own zone) and never a
 * single offset lookup: a transition on the date in question needs both sides' offsets to tell an
 * ordinary time from a skipped or repeated one.
 */
export function localToInstant(date: string, time: string, tz: string): string {
  const [y, m, d] = date.split("-").map(Number);
  const [hh, mm] = time.split(":").map(Number);
  const local = Date.UTC(y, m - 1, d, hh, mm);

  const offsetBefore = offsetAt(local - DAY_MS, tz);
  const offsetAfter = offsetAt(local + DAY_MS, tz);

  if (offsetBefore === offsetAfter) {
    // No transition within a day of this wall-clock reading: one offset, no ambiguity.
    return new Date(local - offsetBefore).toISOString();
  }

  const candidateBefore = local - offsetBefore;
  const candidateAfter = local - offsetAfter;
  // A candidate is real only if the zone, AT that candidate instant, actually reports the offset
  // used to build it - otherwise it's a guess about the wrong side of the transition.
  const consistentBefore = offsetAt(candidateBefore, tz) === offsetBefore;
  const consistentAfter = offsetAt(candidateAfter, tz) === offsetAfter;

  if (consistentBefore && consistentAfter) {
    // Repeated wall-clock time (fall-back): both readings are real. First occurrence = earlier.
    return new Date(Math.min(candidateBefore, candidateAfter)).toISOString();
  }
  if (consistentBefore) return new Date(candidateBefore).toISOString();
  if (consistentAfter) return new Date(candidateAfter).toISOString();
  // Skipped wall-clock time (spring-forward): neither reading is real. Use the offset from
  // before the change, per the server's rule.
  return new Date(candidateBefore).toISOString();
}

export type BlockForm = {
  /** true = block whole days (a date range); false = block part of one day (a start/end time). */
  allDay: boolean;
  firstDay: string;
  lastDay: string;
  startTime: string;
  endTime: string;
  reason: string;
  tz: string;
};

/** `TimeOffIn`'s exactly-one-pair shape, decided by the form's own `allDay` flag - never derived
 * from which fields happen to be filled in, so a leftover time value from a toggle never leaks
 * into a whole-day request. A blank (or whitespace-only) reason is left out entirely: the field is
 * optional, but the server's `Reason` type refuses an empty string (min_length=1), so sending one
 * on CREATE would turn "no reason" into a 422 rather than into nothing at all. (An EDIT that needs
 * to CLEAR a reason uses `patchBody` below, which sends an explicit `null` instead.) */
export function requestBody(
  form: BlockForm,
): { first_day: string; last_day: string; reason?: string } | { starts_at: string; ends_at: string; reason?: string } {
  const reason = form.reason.trim();
  const withReason = <T extends object>(body: T): T & { reason?: string } => (reason ? { ...body, reason } : body);
  if (form.allDay) return withReason({ first_day: form.firstDay, last_day: form.lastDay });
  return withReason({
    starts_at: localToInstant(form.firstDay, form.startTime, form.tz),
    ends_at: localToInstant(form.firstDay, form.endTime, form.tz),
  });
}

export type Patch = {
  reason?: string | null;
  first_day?: string;
  last_day?: string;
  starts_at?: string;
  ends_at?: string;
};

/**
 * PATCH `TimeOffChange`'s partial shape: only the fields the person actually changed, since the
 * server merges any subset within the block's own kind (`time_off.py`'s `TimeOffChange`). This is
 * what makes editing just the reason on a multi-day partial block leave its times untouched
 * (fence: the old "always resend the whole pair" body silently shortened it to one day). A blank
 * reason, when it changed, is sent as an explicit `null` (never omitted): the server only clears a
 * reason on `null`, and omitting the field would leave the old one in place.
 *
 * Switching kind (`allDay` flipped) is the one case that must send the complete new pair - the
 * server refuses a partial pair from the other kind - so it ignores which day/time fields did or
 * didn't change and sends every field of the new kind.
 */
export function patchBody(initial: BlockForm, current: BlockForm): Patch {
  const patch: Patch = {};
  if (current.reason.trim() !== initial.reason.trim()) patch.reason = current.reason.trim() || null;

  if (current.allDay !== initial.allDay) {
    if (current.allDay) {
      patch.first_day = current.firstDay;
      patch.last_day = current.lastDay;
    } else {
      patch.starts_at = localToInstant(current.firstDay, current.startTime, current.tz);
      patch.ends_at = localToInstant(current.firstDay, current.endTime, current.tz);
    }
    return patch;
  }

  if (current.allDay) {
    if (current.firstDay !== initial.firstDay) patch.first_day = current.firstDay;
    if (current.lastDay !== initial.lastDay) patch.last_day = current.lastDay;
    return patch;
  }

  const dateChanged = current.firstDay !== initial.firstDay;
  if (dateChanged || current.startTime !== initial.startTime) {
    patch.starts_at = localToInstant(current.firstDay, current.startTime, current.tz);
  }
  if (dateChanged || current.endTime !== initial.endTime) {
    patch.ends_at = localToInstant(current.firstDay, current.endTime, current.tz);
  }
  return patch;
}

/** "Today" as a plain YYYY-MM-DD, in `tz` rather than the runner's own zone. */
function localDateISO(instant: Date, tz: string): string {
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(instant);
  const map = Object.fromEntries(parts.map((p) => [p.type, p.value]));
  return `${map.year}-${map.month}-${map.day}`;
}

/** A calendar date `days` after `dateISO`, plain date arithmetic (no zone involved, and never a
 * fixed 24h-per-day offset: a calendar day near a DST change can be 23 or 25 real hours, so a
 * caller adding `n * 86_400_000` ms can land on the wrong calendar date entirely). */
function addDaysISO(dateISO: string, days: number): string {
  const [y, m, d] = dateISO.split("-").map(Number);
  const date = new Date(Date.UTC(y, m - 1, d + days));
  return `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, "0")}-${String(date.getUTCDate()).padStart(2, "0")}`;
}

/**
 * The blocked-time list window: `[today, today + horizonDays)` in the business zone,
 * the same window clients can book in (`GET /api/settings.booking_horizon_days`) - never a
 * hardcoded year, never the runner's own zone for "today", and never a fixed-ms "n days later"
 * for the far end. `now` and `horizonDays` are both passed in, so the fence controls them exactly
 * rather than reading the real clock or a real settings read.
 */
export function listWindow(now: Date, tz: string, horizonDays: number): { from: string; to: string } {
  const today = localDateISO(now, tz);
  return { from: localToInstant(today, "00:00", tz), to: localToInstant(addDaysISO(today, horizonDays), "00:00", tz) };
}

/** Whether a just-saved block's own effective start falls at or past the list window's `to`
 * (from `listWindow`): it saved fine, but it won't show up in the list the person is looking at,
 * since that list is capped to the booking horizon. */
export function beyondHorizon(block: { starts_at: string | null; first_day: string | null }, tz: string, windowTo: string): boolean {
  const effectiveStart = block.starts_at ?? localToInstant(block.first_day!, "00:00", tz);
  return new Date(effectiveStart).getTime() >= new Date(windowTo).getTime();
}

export type Block = { starts_at: string | null; ends_at: string | null; first_day: string | null; last_day: string | null };
export type BlockLabel =
  | { kind: "day"; date: string }
  | { kind: "range"; from: string; to: string; days: number }
  | { kind: "partial"; date: string; start: string; end: string }
  | { kind: "partialRange"; from: string; to: string; start: string; end: string };

/** "HH:MM" in `tz`, from an instant. */
function localTime(instant: string, tz: string): string {
  return new Intl.DateTimeFormat("en-GB", { timeZone: tz, hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(new Date(instant));
}

/** The number of inclusive whole days between two YYYY-MM-DD dates. */
function inclusiveDays(first: string, last: string): number {
  const [y1, m1, d1] = first.split("-").map(Number);
  const [y2, m2, d2] = last.split("-").map(Number);
  return Math.round((Date.UTC(y2, m2 - 1, d2) - Date.UTC(y1, m1 - 1, d1)) / 86_400_000) + 1;
}

/**
 * The list row's shape for one block: a whole-day block (`starts_at`/`ends_at` are null - the
 * server never invents a midnight for these) is a day or a range, from its own `first_day`/
 * `last_day` fields alone, never from time arithmetic on a null instant (the bug this fences:
 * formatting `null` as a time reads "00:00", not "no time at all"). A partial block reads its
 * date(s) and its start/end clock time from the instants, in the business zone, never UTC (a
 * second bug this fences: a partial block just before midnight in the business zone, but not yet
 * midnight UTC, would otherwise show the wrong day). A partial block whose start and end fall on
 * different local dates - possible via the API directly, or from data made before whole-day
 * blocks existed - shows both dates (`partialRange`), never just the start day with the end
 * silently dropped. The caller turns this into words: `Console.timeOff` supplies "Whole day",
 * "{n} whole days", the day/range date formatting (via `dateLocale`) and the "{from} – {to}" join.
 */
export function blockLabel(block: Block, tz: string): BlockLabel {
  if (block.first_day !== null && block.last_day !== null) {
    if (block.first_day === block.last_day) return { kind: "day", date: block.first_day };
    return { kind: "range", from: block.first_day, to: block.last_day, days: inclusiveDays(block.first_day, block.last_day) };
  }
  const startsAt = block.starts_at;
  if (startsAt === null || block.ends_at === null) throw new Error("a block has neither pair filled in");
  const start = localDateISO(new Date(startsAt), tz);
  const end = localDateISO(new Date(block.ends_at), tz);
  const times = { start: localTime(startsAt, tz), end: localTime(block.ends_at, tz) };
  if (start === end) return { kind: "partial", date: start, ...times };
  return { kind: "partialRange", from: start, to: end, ...times };
}

/** Mirrors `members.may_manage` (`time_off.py`'s `manual_block`): an owner manages everyone's
 * block, anyone else only their own. A `source: "google"` block is never editable by anyone - the
 * API's PATCH/DELETE 404 on it (`time_off.py:87`, `manual_block`'s `source = 'manual'` filter), so
 * the row only ever links out to Google Calendar. */
export function canEditBlock(role: "owner" | "worker", isSelf: boolean, source: "manual" | "google"): boolean {
  if (source === "google") return false;
  return role === "owner" || isSelf;
}

/** Every field a partial block's date/time inputs need present before `localToInstant` can run:
 * fences the bug where an empty date or time reached `localToInstant` after the in-flight flag
 * was already set, throwing a `RangeError` (from `NaN` inside `Date.UTC`) and leaving the form
 * stuck on "Sending" forever, because nothing downstream ever caught it. */
export function clientProblem(
  form: BlockForm,
): "missingFirstDay" | "missingLastDay" | "missingStart" | "missingEnd" | "lastDayBeforeFirst" | "endNotAfterStart" | null {
  if (!form.firstDay) return "missingFirstDay";
  if (form.allDay) {
    if (!form.lastDay) return "missingLastDay";
    return form.lastDay < form.firstDay ? "lastDayBeforeFirst" : null;
  }
  if (!form.startTime) return "missingStart";
  if (!form.endTime) return "missingEnd";
  return form.endTime <= form.startTime ? "endNotAfterStart" : null;
}
