"use client";

import { useTranslations } from "next-intl";
import { type FormEvent, useState } from "react";

import { dateLocale } from "@/lib/console";
import { changedDays, type Day, daysFromShifts, daysSummary, type Shift, weekBody, weekdayName, weekProblems } from "@/lib/week";

import { SignedOutBanner } from "./console";
import styles from "./console.module.css";
import { Banner, FieldError, type Outcome, problem, Submit } from "./parts";
import uiStyles from "./ui.module.css";

type T = ReturnType<typeof useTranslations>;

type Phase = "idle" | "saving" | "saved" | "error";
type Status = Phase | "dirty";

/** The shift on a day whose start (or end) overlaps another, and the exact overlap window: the
 * drawn field error names it ("cover 13:00 to 14:00 twice"). Presentation only, not exported: the
 * real overlap *check* is `weekProblems`, this only finds which pair to name in the message. */
function overlapWindow(shifts: Shift[]): { from: string; to: string } | null {
  const sorted = [...shifts].sort((a, b) => (a.start < b.start ? -1 : a.start > b.start ? 1 : 0));
  for (let i = 1; i < sorted.length; i++) {
    if (sorted[i].start < sorted[i - 1].end) return { from: sorted[i].start, to: sorted[i - 1].end };
  }
  return null;
}

function shiftLabel(kind: "opensAt" | "closesAt", weekday: string, index: number, total: number, t: T): string {
  if (total === 1) return t(kind, { weekday });
  if (index === 0) return t(`${kind}First`, { weekday });
  if (index === 1) return t(`${kind}Second`, { weekday });
  return t(`${kind}Block`, { weekday, n: index + 1 });
}

type ApiShift = { weekday: number; starts_at: string; ends_at: string };

type WeekEditorProps = {
  initial: Day[];
  /** The opening-hours envelope working hours must fall inside (PR 4). `null` here in PR 2: the
   * opening week itself has no envelope. */
  envelope: Day[] | null;
  locale: string;
  /** `Console.opening` (PR 2) or its PR 4 equivalent: the page-specific copy. */
  t: T;
  /** `Console.week`: the shared savebar, error and day-editing copy. */
  tWeek: T;
  onSave(body: ApiShift[]): Promise<Outcome<ApiShift[]>>;
  savedMessage: string;
};

export function WeekEditor({ initial, envelope, locale, t, tWeek, onSave, savedMessage }: WeekEditorProps) {
  const form = useTranslations("Form");
  const [committed, setCommitted] = useState(initial);
  const [days, setDays] = useState(initial);
  const [phase, setPhase] = useState<Phase>("idle");
  const [problems, setProblems] = useState<ReturnType<typeof weekProblems>>({ byDay: {} });
  const [writeFailure, setWriteFailure] = useState<Outcome<unknown> | null>(null);

  const changed = changedDays(committed, days);
  const isDirty = changed.length > 0;
  const busy = phase === "saving";
  const status: Status = busy ? "saving" : phase === "saved" && !isDirty ? "saved" : phase === "error" ? "error" : isDirty ? "dirty" : "idle";

  function edit(next: Day[]) {
    setDays(next);
    if (phase !== "saving") {
      setPhase("idle");
      setProblems({ byDay: {} });
      setWriteFailure(null);
    }
  }

  function mapDay(weekday: number, fn: (day: Day) => Day) {
    edit(days.map((day) => (day.weekday === weekday ? fn(day) : day)));
  }

  function addTimes(weekday: number) {
    mapDay(weekday, (day) => ({ ...day, shifts: [{ start: "", end: "" }] }));
  }

  function addBlock(weekday: number) {
    mapDay(weekday, (day) => ({ ...day, shifts: [...day.shifts, { start: "", end: "" }] }));
  }

  function removeShift(weekday: number, index: number) {
    mapDay(weekday, (day) => ({ ...day, shifts: day.shifts.filter((_, i) => i !== index) }));
  }

  function updateShift(weekday: number, index: number, field: "start" | "end", value: string) {
    mapDay(weekday, (day) => ({ ...day, shifts: day.shifts.map((s, i) => (i === index ? { ...s, [field]: value } : s)) }));
  }

  function copyToEveryDay(weekday: number) {
    const source = days.find((d) => d.weekday === weekday);
    if (!source) return;
    edit(days.map((day) => ({ ...day, shifts: source.shifts.map((s) => ({ ...s })) })));
  }

  function undo() {
    setDays(committed);
    setPhase("idle");
    setProblems({ byDay: {} });
    setWriteFailure(null);
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (status === "idle") return;
    const result = weekProblems(days, envelope);
    if (Object.keys(result.byDay).length > 0 || result.overall) {
      setProblems(result);
      setWriteFailure(null);
      setPhase("error");
      return;
    }
    setPhase("saving");
    const outcome = await onSave(weekBody(days));
    if (outcome.status === 200 && outcome.data) {
      const next = daysFromShifts(outcome.data);
      setCommitted(next);
      setDays(next);
      setProblems({ byDay: {} });
      setWriteFailure(null);
      setPhase("saved");
    } else {
      setWriteFailure(outcome);
      setPhase("error");
    }
  }

  const firstOpenWeekday = days.find((d) => d.shifts.length > 0)?.weekday;
  const problemWeekdays = Object.keys(problems.byDay)
    .map(Number)
    .sort((a, b) => a - b);
  const genericFailure = writeFailure && writeFailure.status !== 401 && problems.overall !== "opening_hours_required" && problemWeekdays.length === 0;

  return (
    <form className={uiStyles.stack} noValidate onSubmit={onSubmit}>
      {writeFailure?.status === 401 && <SignedOutBanner />}
      {genericFailure && <Banner tone="error">{form(problem(writeFailure!))}</Banner>}
      {problems.overall === "opening_hours_required" && <Banner tone="error">{t("allClosedRefusal")}</Banner>}
      {problemWeekdays.length > 0 && (
        <Banner tone="error">
          {tWeek("notSaved", {
            count: problemWeekdays.length,
            list: new Intl.ListFormat(locale, { type: "conjunction" }).format(
              problemWeekdays.map((weekday) => {
                const code = problems.byDay[weekday];
                const weekdayText = weekdayName(weekday, dateLocale(locale));
                if (code === "overlapping_hours") return tWeek("phraseOverlapping", { weekday: weekdayText });
                if (code === "end_not_after_start") return tWeek("phraseEndNotAfterStart", { weekday: weekdayText });
                return tWeek("phraseTimeRequired", { weekday: weekdayText });
              }),
            ),
          })}
        </Banner>
      )}

      <div className={styles.days}>
        {days.map((day) => {
          const weekdayText = weekdayName(day.weekday, dateLocale(locale));
          const code = problems.byDay[day.weekday];
          const errorId = `day-${day.weekday}-error`;
          const overlap = code === "overlapping_hours" ? overlapWindow(day.shifts) : null;
          return (
            <div className={styles.day} key={day.weekday}>
              <div className={styles.dayHead}>
                <span className={styles.dayName}>{weekdayText}</span>
                {day.shifts.length === 0 ? (
                  <span className={styles.dayClosed}>{t("closed")}</span>
                ) : (
                  firstOpenWeekday === day.weekday && (
                    <button className={uiStyles.textButton} type="button" disabled={busy} onClick={() => copyToEveryDay(day.weekday)}>
                      {t("copyToEveryDay")}
                    </button>
                  )
                )}
              </div>

              {day.shifts.length === 0 ? (
                <button className={uiStyles.textButton} type="button" disabled={busy} onClick={() => addTimes(day.weekday)}>
                  {t("addTimes")}
                </button>
              ) : (
                <>
                  <div className={styles.shifts}>
                    {day.shifts.map((shift, index) => {
                      const invalid =
                        code === "time_required"
                          ? shift.start === "" || shift.end === ""
                          : code === "end_not_after_start"
                            ? shift.end !== "" && shift.start !== "" && shift.end <= shift.start
                            : code === "overlapping_hours";
                      const removeLabel =
                        day.shifts.length === 1
                          ? t("removeOnly", { weekday: weekdayText, from: shift.start, to: shift.end })
                          : t("remove", { weekday: weekdayText, from: shift.start, to: shift.end });
                      return (
                        <div className={styles.shift} key={index}>
                          <label className={uiStyles.srOnly} htmlFor={`shift-${day.weekday}-${index}-start`}>
                            {shiftLabel("opensAt", weekdayText, index, day.shifts.length, t)}
                          </label>
                          <input
                            id={`shift-${day.weekday}-${index}-start`}
                            type="time"
                            step={300}
                            max="23:59"
                            value={shift.start}
                            disabled={busy}
                            aria-invalid={invalid || undefined}
                            aria-describedby={invalid ? errorId : undefined}
                            onChange={(e) => updateShift(day.weekday, index, "start", e.target.value)}
                          />
                          <span className={styles.dash} aria-hidden="true">
                            {"–"}
                          </span>
                          <label className={uiStyles.srOnly} htmlFor={`shift-${day.weekday}-${index}-end`}>
                            {shiftLabel("closesAt", weekdayText, index, day.shifts.length, t)}
                          </label>
                          <input
                            id={`shift-${day.weekday}-${index}-end`}
                            type="time"
                            step={300}
                            max="23:59"
                            value={shift.end}
                            disabled={busy}
                            aria-invalid={invalid || undefined}
                            aria-describedby={invalid ? errorId : undefined}
                            onChange={(e) => updateShift(day.weekday, index, "end", e.target.value)}
                          />
                          <button className={styles.iconOnly} type="button" disabled={busy} onClick={() => removeShift(day.weekday, index)}>
                            <span className={uiStyles.srOnly}>{removeLabel}</span>
                            <svg aria-hidden="true" viewBox="0 0 20 20" width="18" height="18">
                              <path d="M5 5l10 10M15 5L5 15" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
                            </svg>
                          </button>
                        </div>
                      );
                    })}
                  </div>
                  <div className={styles.dayActions}>
                    <button className={uiStyles.textButton} type="button" disabled={busy} onClick={() => addBlock(day.weekday)}>
                      {day.shifts.length === 1 ? t("addSecondBlock") : t("addBlock")}
                    </button>
                  </div>
                  {day.shifts.length === 2 && !code && (
                    <p className={uiStyles.hint}>{t("twoBlockHint", { from: day.shifts[0].end, to: day.shifts[1].start })}</p>
                  )}
                  {code && (
                    <FieldError id={errorId}>
                      {code === "time_required"
                        ? tWeek("fieldTimeRequired")
                        : code === "end_not_after_start"
                          ? tWeek("fieldEndNotAfterStart")
                          : overlap
                            ? tWeek("fieldOverlapping", { from: overlap.from, to: overlap.to })
                            : null}
                    </FieldError>
                  )}
                </>
              )}
            </div>
          );
        })}
      </div>

      <p className={uiStyles.hint}>{t("shortenHint")}</p>

      <div className={styles.savebar}>
        <p className={uiStyles.hint} role={status === "saving" ? "status" : undefined}>
          {status === "saving"
            ? tWeek("savingHint")
            : status === "saved"
              ? savedMessage
              : status === "error"
                ? problemWeekdays.length > 0
                  ? tWeek("attentionHint", { count: problemWeekdays.length })
                  : ""
                : status === "dirty"
                  ? tWeek("unsavedChanges", { days: daysSummary(changed, locale) })
                  : ""}
        </p>
        {status === "dirty" || status === "error" ? (
          <button className={uiStyles.textButton} type="button" onClick={undo}>
            {tWeek("undo")}
          </button>
        ) : null}
        {status === "idle" ? (
          <button className={`${uiStyles.button} ${uiStyles.primary} ${styles.saveButton} ${styles.saveIdle}`} type="submit" aria-disabled="true">
            {tWeek("save")}
          </button>
        ) : (
          <div className={styles.saveButton}>
            <Submit busy={busy} busyLabel={tWeek("saving")}>
              {tWeek("save")}
            </Submit>
          </div>
        )}
      </div>
    </form>
  );
}
