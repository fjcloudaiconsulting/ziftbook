// Pure logic behind the service create/edit form: which name a reader sees (services.py:47-49),
// the create/edit request body and its field errors, and the default-gap hint. Kept free of React
// (and of any sibling import, as lib/week.ts) so node --test can run it directly with no module
// resolution to configure: `parseMoney` (lib/money.ts) is passed in as a callback instead.
export type Locale = "en" | "nl" | "pt";
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

/** create: the business-language name is required (services.py:51 only requires one entry
 * overall, but the create screen always shows the business language and the drawn hint promises
 * it is the fallback everyone else needs). edit: any non-blank entry satisfies the server's rule,
 * so an edit of a service whose only name is another language stays valid — applying create's
 * rule there would lock such a service. */
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
  const nameRequired =
    options.mode === "create" ? !name[options.businessLanguage] : Object.keys(name).length === 0;
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
