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
