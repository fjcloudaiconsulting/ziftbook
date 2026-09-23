// A service's price: display formatting, parsing the price field back to minor units, and the
// prefill/sign helpers the form needs. Always 2 minor units, the same assumption the backend makes
// (services.py:53, countries.py:16-17). Kept free of React so node --test can run it directly.

/** `Intl.NumberFormat(locale, {style: "currency", currency}).format(minor / 100)`: the sign, the
 * grouping and the decimal separator all come from `locale`, never a hardcoded sign or a manual
 * `toFixed`. */
export function formatMoney(minor: number, currency: string, locale: string): string {
  return new Intl.NumberFormat(locale, { style: "currency", currency }).format(minor / 100);
}

/** One `.` or `,` decimal separator, at most 2 decimals, no grouping, 0..1_000_000 minor units
 * (services.py:53). `parseFloat`/`Math.floor(x*100)`/rounding would each mis-parse an edge case
 * this function is fenced against; anything else (grouping, more than 2 decimals, out of range,
 * unparsable) returns null so the caller shows a field error instead of guessing. */
export function parseMoney(text: string): number | null {
  const match = /^(\d+)(?:[.,](\d{1,2}))?$/.exec(text.trim());
  if (!match) return null;
  const [, whole, fraction = ""] = match;
  const minor = Number(whole) * 100 + Number(fraction.padEnd(2, "0"));
  return minor >= 0 && minor <= 1_000_000 ? minor : null;
}

/** The price field's prefill: 2 decimals, no grouping separator, so `parseMoney` always reads
 * back what this wrote (a grouped "1.234,56" would otherwise be refused). */
export function priceText(minor: number, locale: string): string {
  return new Intl.NumberFormat(locale, { useGrouping: false, minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(minor / 100);
}

function parts(minor: number, currency: string, locale: string): Intl.NumberFormatPart[] {
  return new Intl.NumberFormat(locale, { style: "currency", currency }).formatToParts(minor / 100);
}

/** The currency's sign (or its code, when Intl has no sign for it, as CHF in en). */
export function currencySign(minor: number, currency: string, locale: string): string {
  return parts(minor, currency, locale).find((p) => p.type === "currency")?.value ?? currency;
}

/** Whether the sign comes before the amount (true) or after it (false), so the form places
 * `currencySign` on the right side of the input. */
export function signFirst(currency: string, locale: string): boolean {
  const all = parts(0, currency, locale);
  const signIndex = all.findIndex((p) => p.type === "currency");
  const amountIndex = all.findIndex((p) => p.type === "integer");
  return signIndex < amountIndex;
}
