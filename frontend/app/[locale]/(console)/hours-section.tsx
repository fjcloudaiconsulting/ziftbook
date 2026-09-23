"use client";

import { useLocale, useTranslations } from "next-intl";
import { type ReactNode, useEffect, useState } from "react";

import { openingHoursRead, workingHoursRead, workingHoursReplace } from "@/api-client";
import { Link } from "@/i18n/navigation";
import { dateLocale } from "@/lib/console";
import { daysFromShifts, type Day, daysSummary, emptyWeek, envelopeFromShifts, shiftRanges, weekdayName } from "@/lib/week";

import { useConsole } from "../_ui/console";
import { WeekEditor } from "../_ui/week-editor";
import styles from "../_ui/console.module.css";
import { Banner, problem } from "../_ui/parts";
import uiStyles from "../_ui/ui.module.css";

type HoursSectionProps = {
  memberId: string;
  /** The owner viewing any member (including themself, via Team) can always edit. A worker's own
   * `/my-hours` can't, when `settings.workers_edit_own_hours` forbids it. */
  editable: boolean;
  /** Owner-facing wording and the "Change the opening hours" links; the worker's own read-write
   * view says the same things in words instead (no owner-only link). */
  ownerView: boolean;
};

export function HoursSection({ memberId, editable, ownerView }: HoursSectionProps) {
  const { call } = useConsole();
  const locale = useLocale();
  const dl = dateLocale(locale);
  const t = useTranslations("Console.hours");
  const tWeek = useTranslations("Console.week");
  const form = useTranslations("Form");

  const [days, setDays] = useState<Day[] | null>(null);
  const [envelope, setEnvelope] = useState<Day[] | null>(null);
  const [rawEmpty, setRawEmpty] = useState(false);
  const [showEditor, setShowEditor] = useState(false);
  const [failure, setFailure] = useState<ReturnType<typeof problem> | null>(null);

  function load() {
    Promise.all([call(() => workingHoursRead({ path: { member_id: memberId } })), call(() => openingHoursRead())]).then(
      ([hoursOutcome, openingOutcome]) => {
        if (hoursOutcome.status === 200 && hoursOutcome.data && openingOutcome.status === 200 && openingOutcome.data) {
          setFailure(null);
          setEnvelope(envelopeFromShifts(openingOutcome.data));
          const shifts = hoursOutcome.data;
          setRawEmpty(shifts.length === 0);
          setDays(shifts.length === 0 ? emptyWeek() : daysFromShifts(shifts));
        } else {
          setFailure(problem(hoursOutcome.status !== 200 ? hoursOutcome : openingOutcome));
        }
      },
    );
  }

  useEffect(() => {
    load();
    // Re-runs if the frame swaps to a different person (Team -> another person, same mount).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [memberId]);

  /** A server `outside_opening_hours` 422 means the owner narrowed the opening hours after this
   * page loaded: `envelope` (the "Shop open ..." lines `WeekEditor` renders) is now stale. Only
   * re-reads the envelope, not the working hours themselves - the rejected PUT never persisted, so
   * `days` is already what the server holds. */
  function reloadEnvelope() {
    call(() => openingHoursRead()).then((outcome) => {
      if (outcome.status === 200 && outcome.data) setEnvelope(envelopeFromShifts(outcome.data));
    });
  }

  if (failure) {
    return (
      <>
        <Banner tone="error">{form(failure)}</Banner>
        <button className={uiStyles.textButton} type="button" onClick={load}>
          {form("tryAgain")}
        </button>
      </>
    );
  }

  if (!days) return null;

  const openWeekdays = envelope?.filter((d) => d.shifts.length > 0).map((d) => d.weekday) ?? [];
  const formatRange = (from: string, to: string) => tWeek("dayRange", { from, to });
  const changeLink = (chunks: ReactNode) => (
    <Link className={uiStyles.textButton} href="/opening-hours">
      {chunks}
    </Link>
  );

  if (!editable) {
    const blockTimeLink = (chunks: ReactNode) => (
      <Link className={uiStyles.textButton} href="/my-hours/time-off">
        {chunks}
      </Link>
    );
    return (
      <>
        <Banner tone="note">
          {envelope === null
            ? t.rich("lockedNoteUnbounded", { link: blockTimeLink })
            : t.rich("lockedNoteBounded", { days: daysSummary(openWeekdays, dl, formatRange), link: blockTimeLink })}
        </Banner>
        <div className={styles.days}>
          {days.map((day) => (
            <div key={day.weekday} className={styles.day}>
              <div className={styles.dayHead}>
                <span className={styles.dayName}>{weekdayName(day.weekday, dl)}</span>
                {day.shifts.length === 0 ? (
                  <span className={styles.dayClosed}>{t("closed")}</span>
                ) : (
                  <span className={styles.shiftText}>{shiftRanges(day.shifts)}</span>
                )}
              </div>
            </div>
          ))}
        </div>
      </>
    );
  }

  if (rawEmpty && !showEditor) {
    return (
      <>
        <div className={uiStyles.empty}>
          <strong>{t("emptyTitle")}</strong>
          <span>{t("emptyBody")}</span>
          <button className={`${uiStyles.button} ${uiStyles.primary}`} type="button" onClick={() => setShowEditor(true)}>
            {t("emptyCta")}
          </button>
        </div>
        {envelope !== null && (
          <p className={uiStyles.hint}>
            {ownerView
              ? t.rich("emptyHintBounded", { days: daysSummary(openWeekdays, dl, formatRange), link: changeLink })
              : t("emptyHintBoundedWorker", { days: daysSummary(openWeekdays, dl, formatRange) })}
          </p>
        )}
      </>
    );
  }

  return (
    <WeekEditor
      initial={days}
      envelope={envelope}
      locale={locale}
      t={t}
      tWeek={tWeek}
      savedMessage={t("saved")}
      envelopeNote={ownerView ? t.rich("noteBounded", { link: changeLink }) : t("noteBoundedWorker")}
      openingHoursLink={ownerView ? changeLink(t("changeOpeningHours")) : undefined}
      allowEmptyWeek
      onStaleEnvelope={reloadEnvelope}
      onSave={(body) => call(() => workingHoursReplace({ path: { member_id: memberId }, body }), { write: true })}
    />
  );
}
