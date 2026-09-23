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
