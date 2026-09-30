// Pure logic behind the merchant's new-booking, move and block-time panels: where a click on the grid
// lands, the request bodies, the day strip, what a pick means, and which answer says what. Free of React
// so node --test can run it in any runner zone.
import { addDaysISO, localDateISO, localTime, localToInstant } from "./time-off.ts";

/** "HH:MM" of minutes since midnight (1440 reads 00:00). */
export const hhmm = (minutes: number) => `${String(Math.floor((minutes % 1440) / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`;

/** The slot a click `offsetMinutes` below the top of the grid (which starts at `fromHour`) opens: rounded
 * DOWN to the business's slot step, and kept inside the shown hours so the last slot still starts before
 * `toHour`. "HH:MM" of the business zone. */
export function slotAt(offsetMinutes: number, fromHour: number, step: number, toHour: number): string {
  const first = fromHour * 60;
  const last = Math.max(first, toHour * 60 - step);
  const minutes = first + Math.floor(Math.max(0, offsetMinutes) / step) * step;
  return hhmm(Math.min(last, minutes));
}

export type NewClient = { name: string; phone: string; email: string };

/** `POST /api/bookings`: exactly one of client_id / new_client; a blank phone or email is left out (the API
 * refuses an empty string); "anyone" sends no member_id; override only ever goes with a named person. */
export function bookingBody(input: { clientId: string | null; newClient: NewClient | null; serviceId: string; memberId: string | null; startsAt: string; override: boolean }) {
  const body: { client_id?: string; new_client?: { name: string; phone?: string; email?: string }; service_id: string; member_id?: string; starts_at: string; override?: true } = {
    service_id: input.serviceId,
    starts_at: input.startsAt,
  };
  if (input.clientId) {
    body.client_id = input.clientId;
  } else if (input.newClient) {
    const phone = input.newClient.phone.trim();
    const email = input.newClient.email.trim();
    body.new_client = { name: input.newClient.name.trim(), ...(phone && { phone }), ...(email && { email }) };
  }
  if (input.memberId) {
    body.member_id = input.memberId;
    if (input.override) body.override = true;
  }
  return body;
}

/** `POST /api/bookings/{id}/reschedule`: the named person (a move never hands a booking to "anyone"). */
export function moveBody(input: { startsAt: string; memberId: string; override: boolean }) {
  return { starts_at: input.startsAt, member_id: input.memberId, ...(input.override && { override: true as const }) };
}

export type Pick = { kind: "none" } | { kind: "slot"; slot: string } | { kind: "other"; date: string; time: string };

/** What the panel starts on when opened from an empty spot: that start if it is offered, otherwise "Pick
 * another time" filled in with it (never silently lost). */
export function initialPick(at: string | null, slots: string[], tz: string): Pick {
  if (!at) return { kind: "none" };
  const slot = slots.find((s) => Date.parse(s) === Date.parse(at));
  if (slot) return { kind: "slot", slot };
  return { kind: "other", date: localDateISO(new Date(at), tz), time: localTime(at, tz) };
}

/** The instant a pick means, or null while it is unfinished (a blank date or time would throw in
 * `localToInstant`). */
export function pickInstant(pick: Pick, tz: string): string | null {
  if (pick.kind === "slot") return pick.slot;
  if (pick.kind === "other" && pick.date && pick.time) return localToInstant(pick.date, pick.time, tz);
  return null;
}

/** Where the day strip starts: the anchor, never a day in the past. */
export const stripStart = (anchor: string, todayISO: string) => (anchor < todayISO ? todayISO : anchor);

/** Where the strip starts and which day is selected: the start's local day (or the calendar's date), never
 * before today. A past start lands on today, selected; the selected day is always inside the strip. */
export function initialStrip(startDay: string | null, date: string, todayISO: string): { anchor: string; day: string | null } {
  const anchor = stripStart(startDay ?? date, todayISO);
  return { anchor, day: startDay === null ? null : startDay < anchor ? anchor : startDay };
}

/** "Next 7 days" / "Previous 7 days": a week either way, never below today. */
export const shiftStrip = (anchor: string, direction: 1 | -1, todayISO: string) => stripStart(addDaysISO(anchor, direction * 7), todayISO);

/** Offered starts by the local day they fall on, in time order. */
export function groupSlots(slots: string[], tz: string): Record<string, string[]> {
  const grouped: Record<string, string[]> = {};
  for (const slot of [...slots].sort()) (grouped[localDateISO(new Date(slot), tz)] ??= []).push(slot);
  return grouped;
}

/** True only for the latest request's answer: a slow earlier one never overwrites a later one. */
export const isLatest = (mine: number, current: number) => mine === current;

/** The client search text, or null when there is nothing to search for. */
export const searchTerm = (q: string) => q.trim() || null;

/** The client search effect's decision: `call` only for a non-empty term; `apply` an answer only while it is
 * still the latest request (`mine` is its number, `latest` the newest one made). */
export function searchStep(q: string, mine: number, latest: number): { term: string | null; call: boolean; apply: boolean } {
  const term = searchTerm(q);
  return { term, call: term !== null, apply: isLatest(mine, latest) };
}

/** After the service changes: the chosen person stays when they do the new service (or nobody is chosen),
 * otherwise `fallback` (Anyone free for a new booking). */
export function memberForService(withId: string, workerIds: string[], fallback: string): string {
  return !withId || workerIds.includes(withId) ? withId : fallback;
}

/** Whether the calendar's "booked / moved / blocked" hint still belongs to what is on screen. */
export function messageShown(message: { booking: string | null; date: string; view: string } | null, now: { booking: string | null; date: string; view: string; panel: string | null }): boolean {
  return message !== null && !now.panel && message.booking === now.booking && message.date === now.date && message.view === now.view;
}

/** The block form's prefill for a click at `at`: that start in the business zone, an hour long, never past
 * midnight. */
export function blockPrefill(at: string, tz: string): { firstDay: string; startTime: string; endTime: string } {
  const startTime = localTime(at, tz);
  const [h, m] = startTime.split(":").map(Number);
  return { firstDay: localDateISO(new Date(at), tz), startTime, endTime: hhmm(Math.min(h * 60 + m + 60, 23 * 60 + 59)) };
}

/** The service a panel starts on: the first live one (that the prefilled person does, when there is one);
 * null when there is none to offer. */
export function defaultService<S extends { archived: boolean; worker_ids: string[] }>(services: S[], memberId: string | null): S | null {
  return services.find((s) => !s.archived && (!memberId || s.worker_ids.includes(memberId))) ?? null;
}

/** Whether a move's pick is where the booking already is (the button stays disabled). */
export function isUnchanged(startsAt: string | null, memberId: string | null, booking: { starts_at: string; worker_id: string }): boolean {
  return startsAt !== null && Date.parse(startsAt) === Date.parse(booking.starts_at) && memberId === booking.worker_id;
}

export type Failure = "signedOut" | "slotTaken" | "memberUnavailable" | "emailTaken" | "changed" | "ownerOnly" | "problem";

/** What a refused submit says. Without an override any slot 409 means "just taken"; with one, the API says
 * whether the time or the person is the trouble, and the person is only named when one was chosen. `problem`
 * is the console's general banner (a 422 never says which field, so never blame one). */
export function submitFailure(outcome: { status: number; code?: string }, override: boolean, memberName: string | null): Failure {
  if (outcome.status === 401) return "signedOut";
  if (outcome.status === 403) return outcome.code === "owner_only" ? "ownerOnly" : "problem";
  if (outcome.status !== 409) return "problem";
  if (outcome.code === "email_taken") return "emailTaken";
  if (outcome.code === "invalid_transition") return "changed";
  if (!override || outcome.code === "slot_taken") return "slotTaken";
  if (outcome.code === "slot_unavailable") return memberName ? "memberUnavailable" : "slotTaken";
  return "problem";
}
