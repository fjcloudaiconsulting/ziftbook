// Turns lib/time-off.ts's blockLabel() into the words the list row shows: the title ("Friday 3
// October", or a range) and the meta line ("Whole day" / "7 whole days" / "09:00 – 11:00"). Kept
// out of time-off-section.tsx only because it needs both the pure lib and next-intl's `t`.
import { blockLabel, type Block } from "@/lib/time-off";
import { dateLocale } from "@/lib/console";

function dayLabel(date: string, locale: string): string {
  const [y, m, d] = date.split("-").map(Number);
  return new Intl.DateTimeFormat(dateLocale(locale), { weekday: "long", day: "numeric", month: "long", timeZone: "UTC" }).format(
    new Date(Date.UTC(y, m - 1, d)),
  );
}

/** The row's title: a day, a range (via the catalog's "{from} to {to}") - a whole-day one or a
 * partial block spanning days alike - or a single-day partial block's own date (the time sits in
 * the meta line instead). */
export function blockTitle(block: Block, locale: string, tz: string, formatRange: (from: string, to: string) => string): string {
  const label = blockLabel(block, tz);
  if (label.kind === "day") return dayLabel(label.date, locale);
  if (label.kind === "range" || label.kind === "partialRange") return formatRange(dayLabel(label.from, locale), dayLabel(label.to, locale));
  return dayLabel(label.date, locale);
}

/** The row's meta: "Whole day", "{n} whole days", or the part-day time range in the business zone
 * (via the catalog's "{from} – {to}", never a hardcoded separator built in this file). `t` is
 * next-intl's `Console.timeOff` translator; typed loosely here (rather than importing its
 * generated literal-key type) since this is glue, not part of the pure lib. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function blockMeta(block: Block, tz: string, t: (key: any, values?: any) => string): string {
  const label = blockLabel(block, tz);
  if (label.kind === "day") return t("wholeDay");
  if (label.kind === "range") return t("wholeDays", { n: label.days });
  return t("timeRange", { from: label.start, to: label.end });
}
