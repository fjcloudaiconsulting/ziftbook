import type { BookingPageOut } from "@/api-client";
import { type Locale, serviceName } from "@/lib/services";

export type Service = BookingPageOut["services"][number];
export type Worker = Service["workers"][number];

/** The dynamic segment's fallback name+lang: reader locale, else the business's, else null (never
 * shown). `lang` is only set when the shown text is NOT in the reader's own locale (spec §4). */
export function localized(map: Record<string, string>, locale: Locale, businessLanguage: Locale): { text: string; lang: Locale | null } {
  const text = serviceName(map, locale, businessLanguage);
  return { text, lang: map[locale] ? null : businessLanguage };
}

// One flat shape for the whole flow (not a discriminated union): `service` is the only field that
// really gates the steps, and every call site already checks it (or `flow.step`) at runtime, so a
// union bought type-narrowing this file never used, at the cost of casts on every update.
// The checkout has two steps, shown as 1 and 2: `step` 2 is the day and time, 3 the details.
export type Flow = {
  step: 2 | 3;
  service: Service | null;
  worker: string;
  weekStart: string;
  day: string | null;
  slot: string | null;
  // ZIF-100: the step 3 "who" choice, kept apart from the step 2 filter `worker`, and the ids free
  // at `slot` (from the window it was picked in).
  pick: string | null;
  free: string[];
  taken: Set<string>;
  name: string;
  email: string;
  phone: string;
  errors: { name?: string; email?: string; phone?: string; message?: string };
  suggest: string | null;
  banner: { where: 2 | 3 | "c"; tone: "note" | "error"; text: string } | null;
  busy: boolean;
};
// Once `service` is set, these fields are meaningful; this alias just documents
// that at call sites instead of re-asserting it.
export type Step2Plus = Flow & { service: Service };

// `who[i]`: the ids free at `slots[i]`.
export type TimesWindow = { slots: string[]; who: string[][] };
export type CacheEntry = TimesWindow | "error" | "loading";
