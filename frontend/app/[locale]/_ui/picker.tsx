"use client";

import { useLocale, useTranslations } from "next-intl";
import { type ReactNode, useEffect } from "react";

import { localDay, localWhen, pickerDay, slotsByDay, weekDays } from "@/lib/booking-link";
import { groupByDayPart } from "@/lib/booking-page";
import { dateLocale } from "@/lib/console";

import styles from "./ui.module.css";

export function CheckIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 16 16" width="16" height="16">
      <path d="M3 8.5l3 3 7-7" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/** The day and time picker of the booking page and of the emailed link's reschedule screen (ZIF-149):
 * the week with Previous/Next, a strip of seven days, then that day's times by part of the day.
 * `slots` null is still loading (or `failed`, when given); every date and time is the business's (`zone`). */
export function Picker({
  weekStart,
  zone,
  slots,
  day: chosenDay,
  slot: chosen,
  taken,
  nextDisabled = false,
  hint,
  failed,
  empty,
  onWeek,
  onDay,
  onSlot,
}: {
  weekStart: string;
  zone: string;
  slots: string[] | null;
  day: string | null;
  slot: string | null;
  taken: ReadonlySet<string>;
  nextDisabled?: boolean;
  hint?: ReactNode;
  failed?: ReactNode;
  empty?: ReactNode;
  onWeek(delta: number): void;
  onDay(day: string): void;
  onSlot(slot: string): void;
}) {
  const t = useTranslations("BookingPage");
  const dLocale = dateLocale(useLocale());
  const today = localDay(new Date(), zone);
  const days = weekDays(weekStart);
  const dayLabel = (d: string, options: Intl.DateTimeFormatOptions) =>
    new Intl.DateTimeFormat(dLocale, { ...options, timeZone: "UTC" }).format(new Date(`${d}T12:00:00Z`));
  const range = `${dayLabel(days[0], { day: "numeric", month: "short" })} – ${dayLabel(days[6], { day: "numeric", month: "short" })}`;

  const byDay = slotsByDay(slots ?? [], zone);
  const has = (d: string) => (byDay.get(d) ?? []).length > 0;
  const day = pickerDay(days, byDay, chosenDay);

  // Selecting the week's first free day is a consequence of this render, not something to do
  // WHILE rendering (that would call the parent's setState mid-render): an effect, after.
  useEffect(() => {
    if (day && chosenDay !== day) onDay(day);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [day]);

  let body: ReactNode;
  if (!slots) {
    body = failed ?? (
      <p className={styles.hint} role="status">
        {t("loadingTimes")}
      </p>
    );
  } else if (!days.some(has)) {
    body = (
      <div className={styles.empty} role="status">
        <strong>{t("noWeekTitle", { a: dayLabel(days[0], { weekday: "short", day: "numeric", month: "short" }), b: dayLabel(days[6], { weekday: "short", day: "numeric", month: "short" }) })}</strong>
        {empty}
      </div>
    );
  } else if (day) {
    const grouped = groupByDayPart(byDay.get(day) ?? [], zone);
    body = (
      <>
        <div className={styles.days} role="group" aria-label={range}>
          {days.map((d) => {
            const long = dayLabel(d, { weekday: "long", day: "numeric", month: "long" });
            return (
              <button
                key={d}
                className={`${styles.button} ${d === day ? styles.primary : styles.secondary}`}
                type="button"
                disabled={!has(d) || d < today}
                aria-pressed={d === day}
                aria-label={has(d) ? long : `${long}, ${t("closed")}`}
                onClick={() => onDay(d)}
              >
                <span>{dayLabel(d, { weekday: "short" })}</span>
                <span>{dayLabel(d, { day: "numeric" })}</span>
              </button>
            );
          })}
        </div>
        {hint}
        <div className={styles.parts} role="group" aria-label={t("timesOn", { day: dayLabel(day, { weekday: "long", day: "numeric", month: "long" }) })}>
          {(["morning", "afternoon", "evening"] as const)
            .filter((part) => grouped[part].length > 0)
            .map((part) => (
              <div key={part}>
                <h3>{t(part)}</h3>
                <div className={styles.slots}>
                  {grouped[part].map((slot) => {
                    const time = localWhen(slot, zone, dLocale).time;
                    const struck = taken.has(slot);
                    // Chosen = filled AND checked: never by colour alone.
                    const on = !struck && chosen === slot;
                    return (
                      <button
                        key={slot}
                        className={`${styles.button} ${styles.secondary}`}
                        type="button"
                        disabled={struck}
                        aria-pressed={struck ? undefined : on}
                        aria-label={struck ? t("takenLabel", { time }) : undefined}
                        onClick={() => onSlot(slot)}
                      >
                        {on && <CheckIcon />}
                        {time}
                      </button>
                    );
                  })}
                </div>
              </div>
            ))}
        </div>
      </>
    );
  }

  return (
    <div className={styles.picker}>
      <div className={styles.weekHead}>
        <strong>{range}</strong>
        <div className={styles.weekNav}>
          <button className={`${styles.button} ${styles.secondary} ${styles.small}`} type="button" disabled={weekStart <= today} onClick={() => onWeek(-7)}>
            {t("prevWeek")}
          </button>
          <button className={`${styles.button} ${styles.secondary} ${styles.small}`} type="button" disabled={nextDisabled} onClick={() => onWeek(7)}>
            {t("nextWeek")}
          </button>
        </div>
      </div>
      {body}
    </div>
  );
}
