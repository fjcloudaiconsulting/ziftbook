// The owner's settings form: which fields it holds, what changed against the saved values, and the
// option lists. Pure (no React) so node --test runs it directly; bounds mirror the API's
// `BusinessSettings` (tests/settings.test.mjs reads backend/openapi.json to catch drift).

export type Settings = {
  timezone: string;
  language: string;
  auto_confirm: boolean;
  slot_step_minutes: number;
  min_notice_minutes: number;
  booking_horizon_days: number;
  buffer_pct: number;
  pending_ttl_hours: number;
  max_pending_per_email: number;
  free_cancellation_hours: number;
  reschedule_cutoff_hours: number;
  max_reschedules: number;
  cancellation_policy_text: string;
  workers_edit_own_hours: boolean;
  workers_answer_requests: boolean;
  payment_check: boolean;
  contact_email: string | null;
};
export type Key = keyof Settings;
export type Draft = Record<Key, string | boolean>;

/** int: a number input (whole, bounded); select: a numeric select; text: a string; bool: a checkbox;
 * email: one address or none (an empty box is null). */
export type Field = { key: Key; kind: "int" | "select" | "bool" | "text" | "email"; min?: number; max?: number };

/** In page order; `published` is never here (it saves on its own). */
export const FIELDS: Field[] = [
  { key: "timezone", kind: "text" },
  { key: "language", kind: "text" },
  { key: "contact_email", kind: "email" },
  { key: "auto_confirm", kind: "bool" },
  { key: "slot_step_minutes", kind: "select" },
  { key: "min_notice_minutes", kind: "select", min: 0, max: 10080 },
  { key: "booking_horizon_days", kind: "int", min: 1, max: 365 },
  { key: "buffer_pct", kind: "int", min: 0, max: 100 },
  { key: "pending_ttl_hours", kind: "int", min: 1, max: 168 },
  { key: "max_pending_per_email", kind: "int", min: 1, max: 50 },
  { key: "free_cancellation_hours", kind: "int", min: 0, max: 720 },
  { key: "reschedule_cutoff_hours", kind: "int", min: 0, max: 720 },
  { key: "max_reschedules", kind: "int", min: 0, max: 10 },
  // Compared and sent exactly as typed (no trim); the textarea's maxLength and the API cap its length.
  { key: "cancellation_policy_text", kind: "text" },
  { key: "workers_edit_own_hours", kind: "bool" },
  { key: "workers_answer_requests", kind: "bool" },
  { key: "payment_check", kind: "bool" },
];

export const SLOT_STEPS = [5, 10, 15, 20, 30, 60];
const NOTICE_PRESETS = [0, 30, 60, 120, 240, 720, 1440, 2880, 10080];

/** The draft as form values: text for inputs and selects, booleans for checkboxes. Keys the object
 * lacks are skipped, so a partial response rebases only what it carries. */
export function draftFrom(s: Partial<Settings>): Draft {
  const draft: Record<string, string | boolean> = {};
  for (const { key, kind } of FIELDS) {
    if (!(key in s)) continue;
    draft[key] = kind === "bool" ? (s[key] as boolean) : String(s[key] ?? "");
  }
  return draft as Draft;
}

/** A whole number within bounds, or null. Whole digits only: no sign, decimal point or exponent. */
export function parseWhole(text: string, min: number, max: number): number | null {
  const trimmed = text.trim();
  if (!/^\d+$/.test(trimmed)) return null;
  const value = Number(trimmed);
  return value >= min && value <= max ? value : null;
}

// The API's own address rule (backend/app/passwords.py _EMAIL): one address, no names or lists.
const EMAIL = /^[^@\s<>(),;:"\\\x00-\x1f]+@[^@\s<>(),;:"\\\x00-\x1f]+\.[^@\s<>(),;:"\\\x00-\x1f]+$/;

/** The address as the API stores it: trimmed and lowercased. */
function normaliseEmail(text: string): string {
  return text.trim().toLowerCase();
}

// Characters the URL parser would drop or read as URL syntax rather than as part of a host name.
const NOT_A_HOST = /[\s\x00-\x1f\x7f/?#%:@\\[\]]/;

/** The address with an international domain in its ASCII (xn--) form, which is how a mail header
 * carries it and what the API accepts. The same in every browser: Chromium's type=email converts the
 * domain itself, Firefox and Safari do not. Only the domain: the part before the @ has no ASCII
 * form. A domain the URL parser would rewrite (a line break, a slash, a %, or digits it reads as an IP
 * address) is left as typed. */
export function asciiEmail(text: string): string {
  const email = normaliseEmail(text);
  const at = email.lastIndexOf("@");
  const domain = email.slice(at + 1);
  if (at === -1 || !/[^\x00-\x7f]/.test(domain) || NOT_A_HOST.test(domain)) return email;
  try {
    const host = new URL(`http://${domain}`).hostname;
    // A last label with no letter is an address the parser read as an IP (fullwidth digits: １.２ -> 1.0.0.2).
    const ip = !/[a-z]/.test(host.slice(host.lastIndexOf(".") + 1));
    return /^[\x21-\x7e]+$/.test(host) && !ip ? `${email.slice(0, at)}@${host}` : email;
  } catch {
    return email;
  }
}

/** Why the API would refuse this contact email, or null (an empty box is fine: it means none).
 * "accents": a non-ASCII character before the @, which a mail header cannot carry as a plain address
 * (backend/app/business_settings.py header_address); an accented domain is converted (asciiEmail).
 * The API also refuses a few odd shapes the browser does not check (contact_email_invalid). */
export function emailProblem(text: string): "accents" | "invalid" | null {
  const email = asciiEmail(text);
  if (email === "") return null;
  const at = email.lastIndexOf("@");
  if (/[^\x00-\x7f]/.test(at === -1 ? email : email.slice(0, at))) return "accents";
  if (/[^\x00-\x7f]/.test(email) || email.length > 254 || email.includes("=?") || !EMAIL.test(email)) return "invalid";
  return null;
}

/** Which message a refused contact email shows: an accented name its own (the API cannot put it in a
 * mail header), an empty box while a change waits its own (Cancel change drops the waiting address),
 * anything else the usual invalid-email one. */
export function emailMessageKey(text: string, waiting = false): "contactEmailInvalid" | "contactClearWaiting" | "invalidEmail" {
  if (waiting && asciiEmail(text) === "") return "contactClearWaiting";
  return emailProblem(text) === "accents" ? "contactEmailInvalid" : "invalidEmail";
}

/** The value to compare and send, or undefined when the text does not parse. */
function parse(field: Field, value: string | boolean): string | number | boolean | null | undefined {
  if (field.kind === "bool" || field.kind === "text") return value;
  if (field.kind === "email") {
    const email = asciiEmail(String(value ?? ""));
    if (email === "") return null;
    return emailProblem(email) ? undefined : email;
  }
  if (field.kind === "int") return parseWhole(String(value), field.min!, field.max!) ?? undefined;
  const n = Number(value);
  return Number.isInteger(n) ? n : undefined;
}

/** The saved settings as the form compares with them (ZIF-232 x ZIF-99): contact_email is the address waiting for
 * its link, else the confirmed one, else none, from the block's state. settings.contact_email is only ever the
 * confirmed address, so it stands in only before the state is read, or when the API has no such route. */
export function formSaved(settings: Settings, contact: ContactEmailState | null): Settings {
  return contact ? { ...settings, contact_email: contact.requested ?? contact.confirmed ?? null } : settings;
}

/** The keys a refused save marks: only those the API names with their own code (ZIF-99). Its other
 * refusals (invalid_request) do not say which key was wrong, so they mark none. */
export function refusedKeys(status: number, code: string | undefined): Key[] {
  return status === 422 && code === "contact_email_invalid" ? ["contact_email"] : [];
}

/** Keys whose parsed draft value differs from the saved one (unparsable counts as changed), in page order. */
export function changedKeys(saved: Settings, draft: Draft): Key[] {
  return FIELDS.filter((f) => parse(f, draft[f.key]) !== saved[f.key]).map((f) => f.key);
}

/** The PUT body (changed keys that parse) and the changed keys that don't. */
export function settingsBody(
  saved: Settings,
  draft: Draft,
  contact: ContactEmailState | null = null,
): { body: Partial<Settings>; invalid: Key[] } {
  const body: Record<string, unknown> = {};
  const invalid: Key[] = [];
  for (const key of changedKeys(saved, draft)) {
    const value = parse(FIELDS.find((f) => f.key === key)!, draft[key]);
    // An empty box while a change waits is no removal: null would also drop the confirmed address
    // clients see (ZIF-232). Cancel change drops the waiting one.
    if (value === undefined || (key === "contact_email" && value === null && contact?.requested)) invalid.push(key);
    else body[key] = value;
  }
  return { body: body as Partial<Settings>, invalid };
}

/** Only the form's own keys from a response: never `published`. */
export function pickFields(data: Partial<Settings>, keys: Key[] = FIELDS.map((f) => f.key)): Partial<Settings> {
  const out: Record<string, unknown> = {};
  for (const key of keys) if (key in data) out[key] = data[key];
  return out as Partial<Settings>;
}

/** After a save: the sent keys take the server's values, edits made to other keys survive. */
export function mergeSaved(draft: Draft, data: Partial<Settings>, sentKeys: Key[]): Draft {
  return { ...draft, ...draftFrom(pickFields(data, sentKeys)) };
}

/** The presets plus the saved value when it is off the list, sorted, once. */
export function noticeOptions(saved: number): number[] {
  return [...new Set([...NOTICE_PRESETS, saved])].sort((a, b) => a - b);
}

/** Minutes as the largest clean unit: 1440 is 24 hours, 2880 is 2 days. */
export function noticeUnit(min: number): { n: number; unit: "none" | "week" | "day" | "hour" | "minute" } {
  if (min === 0) return { n: 0, unit: "none" };
  if (min % 10080 === 0) return { n: min / 10080, unit: "week" };
  if (min > 1440 && min % 1440 === 0) return { n: min / 1440, unit: "day" };
  if (min % 60 === 0) return { n: min / 60, unit: "hour" };
  return { n: min, unit: "minute" };
}

/** The browser's zone list plus the saved zone when the list lacks it. */
export function zoneOptions(saved: string, list: string[]): string[] {
  return list.includes(saved) ? list : [...list, saved];
}

/** Three start times as minutes after midnight, from 9:00 at the given step. */
export function slotExamples(step: number): number[] {
  return [540, 540 + step, 540 + 2 * step];
}

/** Minutes of break a 60-minute service gets at buffer_pct. */
export function breakMinutes(pct: number): number {
  return Math.ceil((60 * pct) / 100);
}

/** A numeric field's draft value while it parses, else the saved one (what computed hints show). */
export type IntKey =
  | "slot_step_minutes"
  | "buffer_pct"
  | "booking_horizon_days"
  | "pending_ttl_hours"
  | "max_pending_per_email"
  | "free_cancellation_hours"
  | "reschedule_cutoff_hours"
  | "max_reschedules";
export function effective(saved: Settings, draft: Draft, key: IntKey): number {
  const value = parse(FIELDS.find((f) => f.key === key)!, draft[key]);
  return typeof value === "number" ? value : saved[key];
}

export type PaymentsState = "off" | "none" | "onboarding" | "ready";

/** The one button the payments panel shows, or none. `off` (the platform's Stripe keys are unset) and an
 * unsupported country would both answer a click with an error, so they get text only. So would a new
 * account in a currency Stripe does not pay out in for the country (ZIF-102); an account that already
 * exists is still finished or opened, as the API allows. */
export function paymentsAction(state: PaymentsState, countrySupported: boolean, currencySupported: boolean): "connect" | "continue" | "dashboard" | null {
  if (state === "off" || !countrySupported) return null;
  if (state === "none") return currencySupported ? "connect" : null;
  return state === "onboarding" ? "continue" : "dashboard";
}

/** What a refused Connect means (ZIF-102): a currency Stripe does not pay out in for the country, a
 * currency changed while connecting (in another tab), or Stripe not answering. */
export function connectError(outcome: { status: number; code?: string }): "signedOut" | "currency" | "currencyChanged" | "failed" {
  if (outcome.status === 401) return "signedOut";
  if (outcome.status === 409 && outcome.code === "payments_unavailable_currency") return "currency";
  if (outcome.status === 409 && outcome.code === "currency_changed") return "currencyChanged";
  return "failed";
}

/** GET /api/settings/contact-email (ZIF-232), as the generated client types it. */
export type ContactEmailState = {
  state: "none" | "unconfirmed" | "sent" | "confirmed";
  confirmed: string | null;
  requested: string | null;
  link_expired: boolean;
  expires_at: string | null;
};
type CopyKey =
  | "contactUnconfirmed"
  | "contactExpired"
  | "contactSent"
  | "contactConfirmed"
  | "contactWaiting"
  | "contactUntilThen"
  | "contactUntilThenNone";
type Copy = [key: CopyKey, values: Record<string, string>];
export type ContactEmailView = {
  pill: "unconfirmed" | "sent" | "confirmed";
  body: Copy;
  untilThen: Copy | null;
  action: "contactSend" | "contactSendNew" | "contactSendAgain" | null;
  ownerOnly?: true;
  // Owners may drop the waiting address and keep the one in effect.
  cancel?: true;
};

/** What the Settings block says about the contact email, or null when there is none. `requested` waits for its
 * inbox to confirm it and is used for nothing: only `confirmed` is what clients see. Only owners are told the
 * waiting address, and only they may send its link. */
export function contactEmailView(s: ContactEmailState, owner: boolean): ContactEmailView | null {
  if (s.state === "none") return null;
  if (s.state === "confirmed") {
    return { pill: "confirmed", body: ["contactConfirmed", { email: s.confirmed ?? "" }], untilThen: null, action: null };
  }
  const sent = s.state === "sent";
  const untilThen: Copy | null = s.confirmed
    ? ["contactUntilThen", { email: s.confirmed }]
    : sent
      ? ["contactUntilThenNone", {}]
      : null;
  if (!owner || s.requested === null) {
    return { pill: sent ? "sent" : "unconfirmed", body: ["contactWaiting", {}], untilThen, action: null, ownerOnly: true };
  }
  const email = { email: s.requested };
  if (sent) return { pill: "sent", body: ["contactSent", email], untilThen, action: "contactSendAgain", cancel: true };
  return s.link_expired
    ? { pill: "unconfirmed", body: ["contactExpired", email], untilThen, action: "contactSendNew", cancel: true }
    : { pill: "unconfirmed", body: ["contactUnconfirmed", email], untilThen, action: "contactSend", cancel: true };
}

/** What a send or cancel of the waiting contact email answered: done, a message key for the owner, signed out,
 * or reload the state (nothing waits any more). */
export function contactActionOutcome(
  status: number,
  code?: string,
): "done" | "contactTooMany" | "contactTooManyToday" | "contactOwnerOnly" | "contactFailed" | "signedOut" | "reload" {
  if (status === 200) return "done";
  if (status === 429) return code === "contact_email_requests_today" ? "contactTooManyToday" : "contactTooMany";
  if (status === 401) return "signedOut";
  if (status === 403 && code === "owner_only") return "contactOwnerOnly"; // no longer an owner
  if (status === 409) return "reload";
  return "contactFailed";
}

/** Why a save that sent the contact email was refused as a whole (ZIF-232): its link limits (429) or a
 * change that raced it (503); nothing was saved, no other key either. null: not such a refusal. */
export function saveRefusal(status: number, code: string | undefined, sentContact: boolean): "contactTooMany" | "contactTooManyToday" | "busy" | null {
  if (!sentContact) return null;
  if (status === 429) return code === "contact_email_requests_today" ? "contactTooManyToday" : "contactTooMany";
  return status === 503 ? "busy" : null;
}

/** Whether the block shows after reading the state: an API from before the route (404) shows nothing. */
export function contactLoadOutcome(status: number): "show" | "hide" | "failed" {
  if (status === 200) return "show";
  return status === 404 ? "hide" : "failed";
}

/** The page's tabs (ZIF-161), in order; `?tab=<id>` picks one. */
export const TABS = ["business", "bookings", "payments", "team"] as const;
export type Tab = (typeof TABS)[number];

/** The tab each field sits in: a refused field opens its tab, a changed one marks it. */
const TAB_OF: Record<Key, Tab> = {
  timezone: "business",
  language: "business",
  contact_email: "business",
  auto_confirm: "bookings",
  slot_step_minutes: "bookings",
  min_notice_minutes: "bookings",
  booking_horizon_days: "bookings",
  buffer_pct: "bookings",
  pending_ttl_hours: "bookings",
  max_pending_per_email: "bookings",
  free_cancellation_hours: "bookings",
  reschedule_cutoff_hours: "bookings",
  max_reschedules: "bookings",
  cancellation_policy_text: "bookings",
  payment_check: "payments",
  workers_edit_own_hours: "team",
  workers_answer_requests: "team",
};

export function tabOf(key: Key): Tab {
  return TAB_OF[key];
}

/** The tabs holding unsaved changes, in tab order. */
export function dirtyTabs(changed: Key[]): Tab[] {
  return TABS.filter((tab) => changed.some((key) => TAB_OF[key] === tab));
}

/** The tab a URL's query opens: `tab` when it names one, else payments on a return from Stripe (a link
 * made before the return URL carried the tab), else the first. */
export function tabFromQuery(search: string): Tab {
  const params = new URLSearchParams(search);
  const tab = params.get("tab") ?? "";
  if ((TABS as readonly string[]).includes(tab)) return tab as Tab;
  return params.has("stripe") ? "payments" : TABS[0];
}

/** The tab index a key moves to in a tablist of `count` (ARIA tabs pattern), or null for any other key. */
export function tabKeyTarget(key: string, index: number, count: number): number | null {
  if (key === "ArrowRight") return (index + 1) % count;
  if (key === "ArrowLeft") return (index - 1 + count) % count;
  if (key === "Home") return 0;
  if (key === "End") return count - 1;
  return null;
}
