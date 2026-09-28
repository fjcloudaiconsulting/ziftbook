// Plain logic for the guest booking page (/{locale}/booking), kept free of React (and of any sibling
// import) so node --test can run it: where the page starts, which screen each answer leads to,
// which sentence the engine's copy_key reads as, and a week of free times by the business's day.

// What the booking emails mint: <business id>.<secrets.token_urlsafe(32)>, the invite shape.
const TOKEN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.[A-Za-z0-9_-]{43}$/i;

/** No fragment: read the booking with the cookie a link set earlier. A mangled link is not active. */
export function firstStage(token: string | null): "read" | "exchange" | "notActive" {
  if (token === null) return "read";
  return TOKEN.test(token) ? "exchange" : "notActive";
}

export type Screen =
  | "booking"
  | "notActive"
  | "anotherBooking"
  | "termsChanged"
  | "changed"
  | "slotTaken"
  | "notAllowed"
  | "refresh"
  | "retry";

const CONFLICTS: Record<string, Screen> = {
  link_changed: "anotherBooking",
  terms_changed: "termsChanged",
  changed: "changed",
  slot_taken: "slotTaken",
  slot_unavailable: "slotTaken",
  not_allowed: "notAllowed",
  nothing_to_confirm: "refresh",
};

/** Which screen an answer leads to. Every 404 is the same screen (spec §7 screen 3); anything
 * else unplanned keeps the current screen with a retry banner (screen 4). */
export function answerScreen(answer: { status: number; code?: string }): Screen {
  if (answer.status === 200 || answer.status === 204) return "booking";
  if (answer.status === 404) return "notActive";
  if (answer.status === 409 && answer.code && Object.hasOwn(CONFLICTS, answer.code)) return CONFLICTS[answer.code];
  return "retry";
}

const COPY = [
  "full_refund_reschedule",
  "full_refund_no_reschedule",
  "no_refund_reschedule",
  "no_refund_no_reschedule",
  "started",
  "pending",
] as const;
export type CopyKey = (typeof COPY)[number];

/** The engine's sentence (ZIF-55 D9's five keys, plus the route's `pending`); none for a key this
 * page doesn't know, rather than a guess about money. */
export function copyMessage(key: string): CopyKey | null {
  return COPY.find((known) => known === key) ?? null;
}

/** The refund sentence restated on the cancel screens. A pending booking was never paid. */
export function refundMessage(engine: { refund_pct: number; copy_key: string }): "pending" | "refundFull" | "refundNone" {
  if (engine.copy_key === "pending") return "pending";
  return engine.refund_pct === 100 ? "refundFull" : "refundNone";
}

/** "2026-10-30" plus n days, on the calendar alone (no clock, no zone). */
export function addDays(day: string, n: number): string {
  const [y, m, d] = day.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d + n)).toISOString().slice(0, 10);
}

/** The picker's strip: seven days from `first`. */
export function weekDays(first: string): string[] {
  return Array.from({ length: 7 }, (_, i) => addDays(first, i));
}

/** The business's local date of an instant, "2026-10-30". */
export function localDay(instant: string | Date, timeZone: string): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date(instant));
}

/** Free start times grouped by the business's local day, in order. */
export function slotsByDay(slots: string[], timeZone: string): Map<string, string[]> {
  const days = new Map<string, string[]>();
  for (const slot of [...slots].sort()) {
    const day = localDay(slot, timeZone);
    days.set(day, [...(days.get(day) ?? []), slot]);
  }
  return days;
}
