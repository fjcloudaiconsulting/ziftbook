// Plain logic for the admin plan screens (ZIF-78), free of React so node --test can run it.

export type Feature = { key: string; kind: "flag" | "count" | "percent"; default: boolean | number; description: string };
export type PlanForm = Record<string, boolean | string>;

/** Percent features are basis points in the API: 1250 is 12.5%. */
export function formatPercent(bp: number, locale: string): string {
  return new Intl.NumberFormat(locale, { style: "percent", maximumFractionDigits: 2 }).format(bp / 10000);
}

/** The editor's fields for a plan's stored values: the registry's default where the plan sets none. */
export function formFromValues(values: Record<string, boolean | number>, registry: Feature[]): PlanForm {
  const form: PlanForm = {};
  for (const feature of registry) {
    const value = values[feature.key] ?? feature.default;
    form[feature.key] = feature.kind === "flag" ? value === true : feature.kind === "percent" ? String(Number(value) / 100) : String(value);
  }
  return form;
}

/** The values to send, or the key of the first field that isn't valid: counts are whole numbers from 0, percents
 * go to 100 with up to two decimals (a comma is taken as a point) and become basis points. */
export function valuesFromForm(form: PlanForm, registry: Feature[]): { values: Record<string, boolean | number> } | { error: string } {
  const values: Record<string, boolean | number> = {};
  for (const { key, kind } of registry) {
    const raw = form[key];
    if (kind === "flag") {
      values[key] = raw === true;
      continue;
    }
    const text = typeof raw === "string" ? raw.trim() : "";
    if (kind === "count") {
      if (!/^\d{1,9}$/.test(text)) return { error: key };
      values[key] = Number(text);
      continue;
    }
    const match = /^(\d{1,3})(?:[.,](\d{1,2}))?$/.exec(text);
    const bp = match && Number(match[1]) * 100 + Number((match[2] ?? "").padEnd(2, "0"));
    if (bp === null || bp > 10000) return { error: key };
    values[key] = bp;
  }
  return { values };
}
