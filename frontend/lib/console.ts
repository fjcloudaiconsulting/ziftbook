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
