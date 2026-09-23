// The console shell's pure logic: which sections a role sees, which one a path is in, dates in the
// business's own language, and comparing two sessions before a write. Kept free of React so
// node --test can run it directly.

export type Role = "owner" | "worker";

export const SECTIONS = ["today", "calendar", "services", "team", "my-hours", "clients", "opening-hours", "settings"] as const;
export type Section = (typeof SECTIONS)[number];

export type Nav = { sidebar: Section[]; tabs: (Section | "more")[]; more: Section[] };

const OWNER: Nav = {
  sidebar: ["today", "calendar", "services", "team", "clients", "opening-hours", "settings"],
  tabs: ["today", "calendar", "services", "team", "more"],
  more: ["opening-hours", "clients", "settings"],
};

const WORKER: Nav = {
  sidebar: ["today", "calendar", "services", "my-hours"],
  tabs: ["today", "calendar", "services", "my-hours"],
  more: [],
};

/** Which sections a role's sidebar, phone tab bar and More sheet carry. The single source of truth
 * for navigation; role gating (`allowed`) reads it too. */
export function navFor(role: Role): Nav {
  return role === "owner" ? OWNER : WORKER;
}

/** The section a pathname (no locale prefix) belongs to, matched by its first segment so a nested
 * route (a person, a new service) still highlights its parent's nav item. */
export function sectionOf(pathname: string): Section | null {
  if (pathname === "/") return "today";
  const first = pathname.split("/")[1];
  return (SECTIONS as readonly string[]).includes(first) ? (first as Section) : null;
}

/** Whether a role may open a path at all. The API enforces the real rule; this only decides what
 * the UI shows, so a role never even requests a page it can't use. */
export function allowed(role: Role, pathname: string): boolean {
  const section = sectionOf(pathname);
  if (section === null) return true;
  if (!navFor(role).sidebar.includes(section)) return false;
  // A worker reads the services list but can't create or edit one.
  if (role === "worker" && section === "services" && pathname !== "/services") return false;
  return true;
}

/** Whether a role may PUT a member's working hours, mirroring the server's own rule
 * (`schedule.py:152`): an owner changes anyone's; a worker only their own, and only while the
 * business allows it. The UI reads this to decide editable vs read-only; the API enforces it. */
export function canEditHours(role: Role, isSelf: boolean, workersEditOwnHours: boolean): boolean {
  return role === "owner" || (isSelf && workersEditOwnHours);
}

/** "Set a name clients can see" shows only while the name is genuinely unset, never for any other
 * falsy-looking value the API might send. */
export function showSetName(displayName: string | null): boolean {
  return displayName === null;
}

/** en reads dates the British way ("Tuesday 22 September"), as drawn; nl and pt keep the URL
 * locale. Money never uses this: it keeps the URL locale for every language. */
export function dateLocale(locale: string): string {
  return locale === "en" ? "en-GB" : locale;
}

/** "Tuesday 22 September", in the business's own time zone and the reader's language. */
export function todayLabel(now: Date, timeZone: string, locale: string): string {
  return new Intl.DateTimeFormat(dateLocale(locale), { weekday: "long", day: "numeric", month: "long", timeZone }).format(now);
}

/** Whether two session reads are the same person in the same business. A sign-in as someone else in
 * another tab replaces the cookie with neither a 401 nor any other signal, so every write compares
 * against a fresh read first. */
export function sameSession(a: { user_id: string; tenant_id: string }, b: { user_id: string; tenant_id: string }): boolean {
  return a.user_id === b.user_id && a.tenant_id === b.tenant_id;
}

type Identity = { user_id: string; tenant_id: string };
type ReadOutcome = { status: number; data?: Identity };
type SendOutcome<T> = { status: number; data?: T };

export type GuardedWriteResult<T> =
  | { kind: "sent"; outcome: SendOutcome<T> }
  | { kind: "signedOut" }
  | { kind: "mismatch" }
  | { kind: "failed"; outcome: { status: number } };

/**
 * Runs a write only after proving the session hasn't changed underneath it. `send` is a thunk, not
 * an already-started request: a generated SDK call fires its HTTP request the instant it's
 * invoked, so passing an in-flight promise here would let the write reach the server before this
 * ever reads the session — exactly the race this function exists to close. `send` runs at most
 * once, and only once `readSession` has resolved to a match.
 */
export async function guardedWrite<T>(
  readSession: () => Promise<ReadOutcome>,
  current: Identity,
  send: () => Promise<SendOutcome<T>>,
): Promise<GuardedWriteResult<T>> {
  const fresh = await readSession();
  if (fresh.status === 401) return { kind: "signedOut" };
  // A 200 with no usable identity (missing or malformed body) is a failure, never the write's own
  // outcome: it never reaches the sameSession check, let alone send().
  if (fresh.status !== 200 || !fresh.data || !fresh.data.user_id || !fresh.data.tenant_id) {
    return { kind: "failed", outcome: { status: fresh.status } };
  }
  if (!sameSession(current, fresh.data)) return { kind: "mismatch" };
  return { kind: "sent", outcome: await send() };
}

/**
 * The outcome a screen renders for a guarded write. All the mapping lives here, not at the call
 * site, so a "failed" result (the identity check itself came back wrong) can never be reported as
 * if it were the write succeeding: guardedWrite's only route to "failed" with a 200 is the check
 * answering 200 with no usable body, never a real write response.
 */
export function writeOutcome<T>(result: GuardedWriteResult<T>): SendOutcome<T> {
  if (result.kind === "sent") return result.outcome;
  if (result.kind === "signedOut") return { status: 401 };
  if (result.kind === "mismatch") return { status: 0 };
  return { status: result.outcome.status === 200 ? 0 : result.outcome.status };
}
