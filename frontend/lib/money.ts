// A service's price: display formatting, parsing the price field back to minor units, and the
// prefill/sign helpers the form needs. Amounts are in the currency's minor units, as many as the
// currency has (ZIF-102): the four a business can hold (backend/app/currencies.py) all have 2, which
// Intl's digits agree with. Kept free of React so node --test can run it directly.

const MAX_MINOR = 1_000_000; // services.py Amount, in minor units whatever the currency
// The currency's plain symbol for every reader ("$", never "US$"; ZIF-102, owner's ruling), its code
// when it has none. backend/app/mail.py _money writes the same.
const CURRENCY = { style: "currency", currencyDisplay: "narrowSymbol" } as const;

/** The currency's minor units, as Intl knows them (CLDR; ISO 4217's exponent for every currency a
 * business can hold, CONTRIBUTING.md "Money"). Never an assumed 2. */
export function minorUnits(currency: string): number {
  const digits = new Intl.NumberFormat("en", { style: "currency", currency }).resolvedOptions().maximumFractionDigits;
  if (digits === undefined) throw new Error(`no minor units for ${currency}`);
  return digits;
}

/** `Intl.NumberFormat(locale, {style: "currency", currencyDisplay: "narrowSymbol", currency})` of
 * the amount in major units: the sign, the grouping and the decimal separator all come from
 * `locale`, never a hardcoded sign or a manual `toFixed`. */
export function formatMoney(minor: number, currency: string, locale: string): string {
  return new Intl.NumberFormat(locale, { ...CURRENCY, currency }).format(minor / 10 ** minorUnits(currency));
}

/** One `.` or `,` decimal separator, at most as many decimals as the currency has (none for a
 * currency without minor units), no grouping, 0..1_000_000 minor units (services.py:58).
 * `parseFloat`/`Math.floor(x*100)`/rounding would each mis-parse an edge case this function is
 * fenced against; anything else (grouping, too many decimals, out of range, unparsable) returns null
 * so the caller shows a field error instead of guessing. */
export function parseMoney(text: string, currency: string): number | null {
  const units = minorUnits(currency);
  const pattern = units === 0 ? /^(\d+)$/ : new RegExp(`^(\\d+)(?:[.,](\\d{1,${units}}))?$`);
  const match = pattern.exec(text.trim());
  if (!match) return null;
  const [, whole, fraction = ""] = match;
  const minor = Number(whole) * 10 ** units + Number(fraction.padEnd(units, "0") || "0");
  return minor >= 0 && minor <= MAX_MINOR ? minor : null;
}

/** The price field's prefill: the currency's decimals, no grouping separator, so `parseMoney`
 * always reads back what this wrote (a grouped "1.234,56" would otherwise be refused). */
export function priceText(minor: number, currency: string, locale: string): string {
  const units = minorUnits(currency);
  return new Intl.NumberFormat(locale, { useGrouping: false, minimumFractionDigits: units, maximumFractionDigits: units }).format(minor / 10 ** units);
}

function parts(minor: number, currency: string, locale: string): Intl.NumberFormatPart[] {
  return new Intl.NumberFormat(locale, { ...CURRENCY, currency }).formatToParts(minor / 10 ** minorUnits(currency));
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
