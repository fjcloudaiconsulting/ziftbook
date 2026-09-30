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
  max_reschedules: number;
};
export type Key = keyof Settings;
export type Draft = Record<Key, string | boolean>;

/** int: a number input (whole, bounded); select: a numeric select; text: a string; bool: a checkbox. */
export type Field = { key: Key; kind: "int" | "select" | "bool" | "text"; min?: number; max?: number };

/** In page order; `published` is never here (it saves on its own). */
export const FIELDS: Field[] = [
  { key: "timezone", kind: "text" },
  { key: "language", kind: "text" },
  { key: "auto_confirm", kind: "bool" },
  { key: "slot_step_minutes", kind: "select" },
  { key: "min_notice_minutes", kind: "select", min: 0, max: 10080 },
  { key: "booking_horizon_days", kind: "int", min: 1, max: 365 },
  { key: "buffer_pct", kind: "int", min: 0, max: 100 },
  { key: "pending_ttl_hours", kind: "int", min: 1, max: 168 },
  { key: "max_pending_per_email", kind: "int", min: 1, max: 50 },
  { key: "max_reschedules", kind: "int", min: 0, max: 10 },
];

export const SLOT_STEPS = [5, 10, 15, 20, 30, 60];
const NOTICE_PRESETS = [0, 30, 60, 120, 240, 720, 1440, 2880, 10080];

/** The draft as form values: text for inputs and selects, booleans for checkboxes. Keys the object
 * lacks are skipped, so a partial response rebases only what it carries. */
export function draftFrom(s: Partial<Settings>): Draft {
  const draft: Record<string, string | boolean> = {};
  for (const { key, kind } of FIELDS) {
    if (!(key in s)) continue;
    draft[key] = kind === "bool" ? (s[key] as boolean) : String(s[key]);
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

function parse(field: Field, value: string | boolean): string | number | boolean | null {
  if (field.kind === "bool" || field.kind === "text") return value;
  if (field.kind === "int") return parseWhole(String(value), field.min!, field.max!);
  const n = Number(value);
  return Number.isInteger(n) ? n : null;
}

/** Keys whose parsed draft value differs from the saved one (unparsable counts as changed), in page order. */
export function changedKeys(saved: Settings, draft: Draft): Key[] {
  return FIELDS.filter((f) => parse(f, draft[f.key]) !== saved[f.key]).map((f) => f.key);
}

/** The PUT body (changed keys that parse) and the changed keys that don't. */
export function settingsBody(saved: Settings, draft: Draft): { body: Partial<Settings>; invalid: Key[] } {
  const body: Record<string, unknown> = {};
  const invalid: Key[] = [];
  for (const key of changedKeys(saved, draft)) {
    const value = parse(FIELDS.find((f) => f.key === key)!, draft[key]);
    if (value === null) invalid.push(key);
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
export function effective(saved: Settings, draft: Draft, key: "slot_step_minutes" | "buffer_pct"): number {
  const value = parse(FIELDS.find((f) => f.key === key)!, draft[key]);
  return typeof value === "number" ? value : saved[key];
}
