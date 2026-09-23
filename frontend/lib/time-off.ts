// Pure logic for blocked time (PR 6, ZIF-50; window per ZIF-103). The server owns whole-day
// midnight (a request sends a bare `{first_day, last_day}` date pair, never computed here); this
// file only turns a partial block's local date + HH:MM (in the business time zone) into the UTC
// instant the API expects, DST included, and the other pure decisions the blocked-time screens
// need. Kept free of React (and of any sibling import) so node --test can run it directly.

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

/**
 * A partial block's local wall-clock date + time in `tz`, turned into the instant the API takes
 * (`starts_at` / `ends_at`). Never `new Date(\`${date}T${time}\`)` (the browser's own zone) and
 * never a fixed offset: the offset is read from `Intl` at the instant itself, and read again once
 * the first guess is applied, so a DST transition on the date in question (spring-forward or
 * fall-back) resolves to the correct side.
 */
export function localToInstant(date: string, time: string, tz: string): string {
  const [y, m, d] = date.split("-").map(Number);
  const [hh, mm] = time.split(":").map(Number);
  const guess = Date.UTC(y, m - 1, d, hh, mm);
  const first = guess - offsetAt(guess, tz);
  const second = guess - offsetAt(first, tz);
  return new Date(second).toISOString();
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
 * would turn "no reason" into a 422 rather than into nothing at all. */
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

/** "Today" as a plain YYYY-MM-DD, in `tz` rather than the runner's own zone. */
function localDateISO(instant: Date, tz: string): string {
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(instant);
  const map = Object.fromEntries(parts.map((p) => [p.type, p.value]));
  return `${map.year}-${map.month}-${map.day}`;
}

/** A calendar date `days` after `dateISO`, plain date arithmetic (no zone involved: a day is a
 * day, regardless of what the clock does that day). */
function addDaysISO(dateISO: string, days: number): string {
  const [y, m, d] = dateISO.split("-").map(Number);
  const date = new Date(Date.UTC(y, m - 1, d + days));
  return `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, "0")}-${String(date.getUTCDate()).padStart(2, "0")}`;
}

/**
 * The blocked-time list window (ZIF-103): `[today, today + horizonDays)` in the business zone,
 * the same window clients can book in (`GET /api/settings.booking_horizon_days`) - never a
 * hardcoded year. `now` and `horizonDays` are both passed in, so the fence controls them exactly
 * rather than reading the real clock or a real settings read.
 */
export function listWindow(now: Date, tz: string, horizonDays: number): { from: string; to: string } {
  const today = localDateISO(now, tz);
  return { from: localToInstant(today, "00:00", tz), to: localToInstant(addDaysISO(today, horizonDays), "00:00", tz) };
}

export type Block = { starts_at: string | null; ends_at: string | null; first_day: string | null; last_day: string | null };
export type BlockLabel =
  | { kind: "day"; date: string }
  | { kind: "range"; from: string; to: string; days: number }
  | { kind: "partial"; date: string; start: string; end: string };

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
 * formatting `null` as a time reads "00:00", not "no time at all"). A partial block reads its date
 * and its start/end clock time from the instants, in the business zone, never UTC. The caller
 * turns this into words: `Console.timeOff` supplies "Whole day", "{n} whole days", the day/range
 * date formatting (via `dateLocale`) and the "{from} – {to}" join.
 */
export function blockLabel(block: Block, tz: string): BlockLabel {
  if (block.first_day !== null && block.last_day !== null) {
    if (block.first_day === block.last_day) return { kind: "day", date: block.first_day };
    return { kind: "range", from: block.first_day, to: block.last_day, days: inclusiveDays(block.first_day, block.last_day) };
  }
  const startsAt = block.starts_at;
  if (startsAt === null || block.ends_at === null) throw new Error("a block has neither pair filled in");
  return { kind: "partial", date: localDateISO(new Date(startsAt), tz), start: localTime(startsAt, tz), end: localTime(block.ends_at, tz) };
}

/** Mirrors `members.may_manage` (`time_off.py`'s `manual_block`): an owner manages everyone's
 * block, anyone else only their own. A `source: "google"` block is never editable by anyone - the
 * API's PATCH/DELETE 404 on it (`time_off.py:87`, `manual_block`'s `source = 'manual'` filter), so
 * the row only ever links out to Google Calendar. */
export function canEditBlock(role: "owner" | "worker", isSelf: boolean, source: "manual" | "google"): boolean {
  if (source === "google") return false;
  return role === "owner" || isSelf;
}
