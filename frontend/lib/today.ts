// Pure logic behind Today's requests queue and appointments: which business-zone day an instant
// falls on, what is past / current / future for the "now" line, the decline request body, and the
// merge of bookings with blocked time. Kept free of React so node --test can run it directly.
import { addDaysISO, localDateISO, localTime, localToInstant } from "./time-off.ts";

export type DayRelation = "today" | "tomorrow" | "later";

/** Which day an instant falls on, counted in `tz` (the business zone), never the browser's: a
 * request expiring at 01:30 tomorrow in Amsterdam is tomorrow even where it is still today. */
export function dayRelation(instant: string, now: Date, tz: string): DayRelation {
  const day = localDateISO(new Date(instant), tz);
  const today = localDateISO(now, tz);
  if (day === today) return "today";
  return day === addDaysISO(today, 1) ? "tomorrow" : "later";
}

/** The expiry line's parts: the day it falls on and its "HH:MM", both in `tz`. The caller words it. */
export function expiryLabel(expiresAt: string, now: Date, tz: string): { when: DayRelation; time: string } {
  return { when: dayRelation(expiresAt, now, tz), time: localTime(expiresAt, tz) };
}

export type Phase = "past" | "current" | "future";

/** An appointment in progress (start <= now < end) is current, not past: comparing only the start
 * would dim and file away the one the person is in the middle of. Ending exactly now is past. */
export function agendaPhase(item: { start: number; end: number }, now: number): Phase {
  if (item.end <= now) return "past";
  return item.start <= now ? "current" : "future";
}

/** Where the "now" line goes: before the first future item (after any in progress). */
export function nowLineAt(items: { start: number; end: number }[], now: number): number {
  const at = items.findIndex((item) => agendaPhase(item, now) === "future");
  return at === -1 ? items.length : at;
}

/** The request to decline a booking. A blank message is left out entirely (the API refuses an
 * empty one, min_length=1); a tab becomes a space and a carriage return goes, since the API only
 * takes printable text and newlines. */
export function declineBody(message: string): { status: "declined"; message?: string } {
  const text = message.replace(/\t/g, " ").replace(/\r/g, "").trim();
  return text ? { status: "declined", message: text } : { status: "declined" };
}

/** Soonest expiry first (the API orders by start), the id as the tie-break. */
export function sortQueue<T extends { id: string; expires_at: string }>(rows: T[]): T[] {
  return [...rows].sort((a, b) => (a.expires_at < b.expires_at ? -1 : a.expires_at > b.expires_at ? 1 : a.id < b.id ? -1 : 1));
}

export type Answer = "confirmed" | "declined" | "stale";

/** Filters a (re)fetched list through what this session has already answered, so a read that was
 * in flight before an answer can never bring the row back. The queue drops every answered row; the
 * agenda confirms an accepted booking and drops a declined one (a stale one is left as the server says). */
export function applyAnswers<T extends { id: string; status?: string }>(rows: T[], answers: Map<string, Answer>, list: "queue" | "agenda"): T[] {
  if (list === "queue") return rows.filter((row) => !answers.has(row.id));
  return rows.filter((row) => answers.get(row.id) !== "declined").map((row) => (answers.get(row.id) === "confirmed" ? { ...row, status: "confirmed" } : row));
}

type BookingLike = { id: string; starts_at: string; ends_at: string };
type BlockLike = { id: string; member_id: string; starts_at: string | null; ends_at: string | null; first_day: string | null; last_day: string | null };

export type AgendaItem<B extends BookingLike, T extends BlockLike> = { key: string; start: number; end: number } & (
  | { kind: "booking"; booking: B }
  | { kind: "block"; block: T; allDay: boolean; startedBefore: boolean }
);

/** Bookings and blocked time as one list ordered by start. A whole-day block runs local midnight to
 * midnight; one that began before the window is flagged so the row reads "until {end}". A worker
 * sees only their own blocks (their bookings are already the only ones the API sends). */
export function mergeAgenda<B extends BookingLike, T extends BlockLike>(
  bookings: B[],
  blocks: T[],
  tz: string,
  window: { from: string },
  me: { role: "owner" | "worker"; memberId: string },
): AgendaItem<B, T>[] {
  const from = new Date(window.from).getTime();
  const items: AgendaItem<B, T>[] = bookings.map((booking) => ({
    kind: "booking",
    key: booking.id,
    start: new Date(booking.starts_at).getTime(),
    end: new Date(booking.ends_at).getTime(),
    booking,
  }));
  for (const block of blocks) {
    if (me.role === "worker" && block.member_id !== me.memberId) continue;
    const allDay = block.first_day !== null;
    const start = new Date(block.starts_at ?? localToInstant(block.first_day!, "00:00", tz)).getTime();
    const end = new Date(block.ends_at ?? localToInstant(addDaysISO(block.last_day!, 1), "00:00", tz)).getTime();
    items.push({ kind: "block", key: block.id, start, end, block, allDay, startedBefore: start < from });
  }
  return items.sort((a, b) => a.start - b.start || (a.key < b.key ? -1 : 1));
}

/** The nav badge's text: a count over 99 reads "99+". */
export function badgeText(count: number): string {
  return count > 99 ? "99+" : String(count);
}

/** How long ago, for Intl.RelativeTimeFormat: minutes under an hour, hours under a day, else days. */
export function relativeAgo(createdAt: string, now: Date): { value: number; unit: "minute" | "hour" | "day" } {
  const minutes = Math.max(0, Math.floor((now.getTime() - new Date(createdAt).getTime()) / 60_000));
  if (minutes < 60) return { value: -minutes || 0, unit: "minute" }; // `|| 0`: never -0
  if (minutes < 60 * 24) return { value: -Math.floor(minutes / 60), unit: "hour" };
  return { value: -Math.floor(minutes / (60 * 24)), unit: "day" };
}
