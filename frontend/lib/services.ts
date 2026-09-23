// Pure logic behind the service create/edit form: which name a reader sees (services.py:47-49),
// the create/edit request body and its field errors, and the default-gap hint. Kept free of React
// (and of any sibling import, as lib/week.ts) so node --test can run it directly with no module
// resolution to configure: `parseMoney` (lib/money.ts) is passed in as a callback instead.
export type Locale = "en" | "nl" | "pt";
export const LOCALES: Locale[] = ["en", "nl", "pt"];
export type NameMap = Partial<Record<Locale, string>>;

/** The reader's own language, else the business's, else the first one present
 * (services.py:47-49). Never `name[locale] ?? name.en` (that would skip a present business-
 * language name for an absent reader one), and never "first present" ahead of the business
 * language when both exist. */
export function serviceName(name: NameMap, locale: Locale, businessLanguage: Locale): string {
  if (name[locale]) return name[locale] as string;
  if (name[businessLanguage]) return name[businessLanguage] as string;
  const [first] = Object.values(name).filter((v): v is string => Boolean(v));
  return first ?? "";
}

/** Which language tab a name/description field opens on. Owner ruling (2026-09-23): a business's
 * language never decides this — a Dutch business whose owner doesn't speak Dutch must not be
 * steered toward filling in Dutch first. Opens on the viewer's own language when that language
 * already has text (edit); otherwise the first language (in canonical order) that has text
 * (edit, viewer's own language absent); otherwise the viewer's own language itself (create, or an
 * edit with nothing typed anywhere yet — nothing to fall back to). */
export function initialLanguageTab(viewerLocale: Locale, name: NameMap): Locale {
  if (name[viewerLocale]) return viewerLocale;
  const filled = LOCALES.find((locale) => name[locale]);
  return filled ?? viewerLocale;
}

export type ServiceForm = {
  name: NameMap;
  description: Partial<Record<Locale, string>>;
  price: string;
  duration: string;
  gap: "default" | "fixed";
  fixedGap?: string;
};

export type ServiceBody = {
  name: NameMap;
  description?: NameMap;
  price: { amount_minor: number };
  duration_minutes: number;
  buffer_minutes?: number | null;
};

export type ServiceBodyResult = { body: ServiceBody; errors?: never } | { body?: never; errors: Record<string, string> };

/** Any one non-blank name, in any language, satisfies the server's own rule (services.py:51 only
 * requires one entry — not specifically the business language). Owner ruling: create follows the
 * same rule as edit, so an English-only name for a Portuguese business is a valid create, not an
 * error demanding the business language specifically. `serviceName`'s display fallback order
 * (reader's language, then the business's, then any) is unaffected — this only decides which
 * name(s) satisfy validation. */
export function serviceBody(
  form: ServiceForm,
  options: { businessLanguage: Locale; mode: "create" | "edit" },
  parsePrice: (text: string) => number | null,
): ServiceBodyResult {
  const errors: Record<string, string> = {};

  const name: NameMap = {};
  for (const [locale, value] of Object.entries(form.name) as [Locale, string | undefined][]) {
    const trimmed = (value ?? "").trim();
    if (trimmed) name[locale] = trimmed;
  }
  const nameRequired = Object.keys(name).length === 0;
  if (nameRequired) errors.name = "nameRequired";

  const minor = parsePrice(form.price);
  if (minor === null) errors.price = "priceInvalid";

  const duration = Number(form.duration);
  if (!Number.isInteger(duration) || duration < 5 || duration > 720) errors.duration = "durationRange";

  let bufferMinutes: number | null = null;
  if (form.gap === "fixed") {
    const fixed = Number(form.fixedGap);
    if (!Number.isInteger(fixed) || fixed < 0 || fixed > 240) errors.gap = "gapRange";
    else bufferMinutes = fixed;
  }

  if (Object.keys(errors).length > 0) return { errors };

  const description: NameMap = {};
  for (const [locale, value] of Object.entries(form.description) as [Locale, string | undefined][]) {
    const trimmed = (value ?? "").trim();
    if (trimmed) description[locale] = trimmed;
  }

  if (options.mode === "edit") {
    // The PATCH body is never the create body reused: description and buffer_minutes are
    // create-only fields (design-r2.html:905-947), and PATCH applies every field it is sent
    // (services.py:81-91), so including them here would reset them on every edit.
    return { body: { name, price: { amount_minor: minor as number }, duration_minutes: duration } };
  }

  return {
    body: {
      name,
      description,
      price: { amount_minor: minor as number },
      duration_minutes: duration,
      buffer_minutes: bufferMinutes, // "use the business default" is null, never 0 (services.py:77)
    },
  };
}

/** The business's default gap, shown as a hint next to "Use the business default"
 * (business_settings.py:56-58 rounds; this always rounds up, so a gap is never shorter than the
 * percentage promises). */
export function defaultBuffer(durationMinutes: number, percent: number): number {
  return Math.ceil((durationMinutes * percent) / 100);
}

/** The list's live services (owner and worker both filter archived ones out; a worker never sees
 * an Archived group at all). */
export function activeServices<T extends { archived: boolean }>(services: T[]): T[] {
  return services.filter((s) => !s.archived);
}

/** The owner list's `<details>` "Archived (n)" group. */
export function archivedServices<T extends { archived: boolean }>(services: T[]): T[] {
  return services.filter((s) => s.archived);
}

/** Whether a service's "no one assigned" warning pill shows. A worker count can never really be
 * negative, but the check is exact equality, not `<= 0`: a bug that starts treating a negative
 * count as "empty" would still leave the true empty case (0) alone, which is why this needs its
 * own guard rather than trusting `n === 0`'s absence of a fence. */
export function needsWorkerWarning(workerCount: number): boolean {
  return workerCount === 0;
}
