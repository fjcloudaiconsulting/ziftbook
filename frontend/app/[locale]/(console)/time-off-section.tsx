"use client";

import { useLocale, useTranslations } from "next-intl";
import { type FormEvent, useEffect, useRef, useState } from "react";

import { timeOffCreate, timeOffDelete, timeOffList, timeOffUpdate, type TimeOffOut } from "@/api-client";
import { Link } from "@/i18n/navigation";
import { beyondHorizon, canEditBlock, clientProblem, dateOrTimeChanged, listWindow, patchBody, requestBody } from "@/lib/time-off";
import { zoneCity } from "@/lib/week";
import { JSON_WRITE, SignedOutBanner, useConsole } from "../_ui/console";
import styles from "../_ui/console.module.css";
import { Banner, FieldError, Heading, Mark, problem, Submit } from "../_ui/parts";
import uiStyles from "../_ui/ui.module.css";
import { blockMeta, blockTitle } from "./time-off-labels";

/** The two-tab bar on a person's frame (working hours / blocked time), shared by the owner's
 * `/team/[memberId]` and the worker's `/my-hours` frames. The label is one catalog string (never
 * two translated fragments joined with a literal "/" here), so a translator owns the whole phrase. */
export function PersonTabs({ workingHoursHref, blockedTimeHref, active }: { workingHoursHref: string; blockedTimeHref: string; active: "hours" | "timeOff" }) {
  const t = useTranslations("Console.timeOff");
  return (
    <div className={styles.tabs} role="tablist" aria-label={t("tabsLabel")}>
      <Link href={workingHoursHref} className={styles.subTab} role="tab" aria-selected={active === "hours"}>
        {t("tabWorkingHours")}
      </Link>
      <Link href={blockedTimeHref} className={styles.subTab} role="tab" aria-selected={active === "timeOff"}>
        {t("tabBlockedTime")}
      </Link>
    </div>
  );
}

type FormState = {
  allDay: boolean;
  firstDay: string;
  lastDay: string;
  startTime: string;
  endTime: string;
  reason: string;
};

function blank(): FormState {
  return { allDay: true, firstDay: "", lastDay: "", startTime: "", endTime: "", reason: "" };
}

/** Whether an editing block's own dates - as read back from the server, not from the form - are a
 * partial block spanning more than one local day (`lib/time-off.ts`'s `partialRange`), so the form
 * can warn before an edit to its times silently collapses it to one day (this form's own fields
 * can only hold a single first-day). */
function isMultiDayPartial(block: TimeOffOut, tz: string): boolean {
  if (block.first_day || !block.starts_at || !block.ends_at) return false;
  const date = (instant: string) => new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date(instant));
  return date(block.starts_at) !== date(block.ends_at);
}

function fromBlock(block: TimeOffOut, tz: string): FormState {
  if (block.first_day) {
    return { allDay: true, firstDay: block.first_day, lastDay: block.last_day ?? block.first_day, startTime: "09:00", endTime: "17:00", reason: block.reason ?? "" };
  }
  const startsAt = new Date(block.starts_at!);
  const date = new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" }).format(startsAt);
  const time = (instant: string) => new Intl.DateTimeFormat("en-GB", { timeZone: tz, hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(new Date(instant));
  return { allDay: false, firstDay: date, lastDay: date, startTime: time(block.starts_at!), endTime: time(block.ends_at!), reason: block.reason ?? "" };
}

type Mode = { kind: "list" } | { kind: "new" } | { kind: "edit"; block: TimeOffOut };

export function BlockedTime({
  memberId,
  personName,
  onFormMode,
}: {
  memberId: string;
  personName: string;
  /** Whether the block list or the block form is showing, so a caller with its own person header
   * (name, tabs) can hide it while the form's own heading is on screen - the form is never behind
   * a second `<h1>`. */
  onFormMode?: (inForm: boolean) => void;
}) {
  const { session, settings, call } = useConsole();
  const locale = useLocale();
  const t = useTranslations("Console.timeOff");
  const tWeek = useTranslations("Console.week");
  const form = useTranslations("Form");
  const isSelf = memberId === session.member_id;
  const role = session.role === "owner" ? "owner" : "worker";

  const [blocks, setBlocks] = useState<TimeOffOut[] | null>(null);
  const [failure, setFailure] = useState<ReturnType<typeof problem> | null>(null);
  const [mode, setMode] = useState<Mode>({ kind: "list" });
  const [overflowNote, setOverflowNote] = useState(false);

  useEffect(() => {
    onFormMode?.(mode.kind !== "list");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode.kind]);

  function load() {
    const window = listWindow(new Date(), settings.timezone, settings.booking_horizon_days);
    call(() => timeOffList({ path: { member_id: memberId }, query: window })).then((outcome) => {
      if (outcome.status === 200 && outcome.data) {
        setFailure(null);
        setBlocks(outcome.data);
      } else {
        setFailure(problem(outcome));
      }
    });
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [memberId]);

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

  if (!blocks) return null;

  if (mode.kind !== "list") {
    return (
      <BlockedTimeForm
        memberId={memberId}
        tz={settings.timezone}
        personName={personName}
        editing={mode.kind === "edit" ? mode.block : null}
        onDone={(saved) => {
          const window = listWindow(new Date(), settings.timezone, settings.booking_horizon_days);
          setOverflowNote(saved !== null && beyondHorizon(saved, settings.timezone, window.to));
          setMode({ kind: "list" });
          load();
        }}
        onCancel={() => setMode({ kind: "list" })}
      />
    );
  }

  const days = settings.booking_horizon_days;

  if (blocks.length === 0) {
    return (
      <>
        {overflowNote && <Banner tone="note">{t("savedBeyondHorizon", { days })}</Banner>}
        <div className={uiStyles.empty}>
          <Mark icon="calendar" />
          <strong>{t("emptyTitle")}</strong>
          <span>{t("emptyBody")}</span>
          <button className={`${uiStyles.button} ${uiStyles.primary}`} type="button" onClick={() => setMode({ kind: "new" })}>
            {t("blockTime")}
          </button>
        </div>
        <p className={uiStyles.hint}>{t("emptyHint", { days })}</p>
      </>
    );
  }

  return (
    <>
      {overflowNote && <Banner tone="note">{t("savedBeyondHorizon", { days })}</Banner>}
      <div className={styles.screenHead}>
        <p className={uiStyles.hint}>{t("listHint", { days })}</p>
        <button className={`${uiStyles.button} ${uiStyles.primary} ${styles.small}`} type="button" onClick={() => setMode({ kind: "new" })}>
          {t("blockTime")}
        </button>
      </div>
      <ul className={styles.list}>
        {blocks.map((block) => {
          const editable = canEditBlock(role, isSelf, block.source);
          const fromGoogle = block.source === "google";
          const title = blockTitle(block, locale, settings.timezone, (from, to) => tWeek("dayRange", { from, to }));
          const meta = blockMeta(block, settings.timezone, t);
          if (!editable) {
            return (
              <li key={block.id}>
                <div className={styles.rowStatic}>
                  <span className={styles.rowMain}>
                    <span className={styles.rowTitle}>{title}</span>
                    <span className={styles.rowMeta}>
                      {meta}
                      {fromGoogle && ` · ${t("changeInGoogle")}`}
                      {!fromGoogle && block.reason ? ` · ${block.reason}` : ""}
                    </span>
                    {fromGoogle && <span className={`${styles.pill} ${styles.pillMuted}`}>{t("fromGoogle")}</span>}
                  </span>
                </div>
              </li>
            );
          }
          return (
            <li key={block.id}>
              <button className={styles.rowLink} type="button" onClick={() => setMode({ kind: "edit", block })}>
                <span className={styles.rowMain}>
                  <span className={styles.rowTitle}>{title}</span>
                  <span className={styles.rowMeta}>
                    {meta}
                    {block.reason ? ` · ${block.reason}` : ""}
                  </span>
                </span>
                <Chevron />
              </button>
            </li>
          );
        })}
      </ul>
      <p className={uiStyles.hint}>{t("reasonListHint")}</p>
    </>
  );
}

function Chevron() {
  return (
    <svg className={styles.chev} aria-hidden="true" viewBox="0 0 12 12" width="12" height="12">
      <path d="M4.5 2.5l3 3.5-3 3.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/** The field-level error's catalog key for every reason `clientProblem` can refuse a submit. */
const FIELD_ERROR_KEY = {
  missingFirstDay: "errorMissingFirstDay",
  missingLastDay: "errorMissingLastDay",
  missingStart: "errorMissingStart",
  missingEnd: "errorMissingEnd",
  lastDayBeforeFirst: "errorLastDayBeforeFirst",
  endNotAfterStart: "errorEndNotAfterStart",
} as const;

function BlockedTimeForm({
  memberId,
  tz,
  personName,
  editing,
  onDone,
  onCancel,
}: {
  memberId: string;
  tz: string;
  personName: string;
  editing: TimeOffOut | null;
  onDone(saved: TimeOffOut | null): void;
  onCancel(): void;
}) {
  const { call, settings } = useConsole();
  const t = useTranslations("Console.timeOff");
  const tConsole = useTranslations("Console");
  const form = useTranslations("Form");
  const [initial] = useState<FormState>(() => (editing ? fromBlock(editing, tz) : blank()));
  const [state, setState] = useState<FormState>(initial);
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [bannerError, setBannerError] = useState<string | null>(null);
  const [signedOut, setSignedOut] = useState(false);
  const [saving, setSaving] = useState(false);
  const [confirmingRemove, setConfirmingRemove] = useState(false);
  const [removing, setRemoving] = useState(false);
  const submitting = useRef(false);
  const confirmRef = useRef<HTMLButtonElement>(null);
  const wasMultiDayPartial = editing ? isMultiDayPartial(editing, tz) : false;
  const dateOrTimeTouched = !state.allDay && dateOrTimeChanged({ ...initial, tz }, { ...state, tz });

  useEffect(() => {
    if (confirmingRemove) confirmRef.current?.focus();
  }, [confirmingRemove]);

  function resetMessages() {
    setFieldError(null);
    setBannerError(null);
    setSignedOut(false);
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (submitting.current) return;
    resetMessages();
    const clientIssue = clientProblem(
      { ...state, lastDay: state.allDay ? state.lastDay : state.firstDay, tz },
      editing ? { ...initial, lastDay: initial.allDay ? initial.lastDay : initial.firstDay, tz } : undefined,
    );
    if (clientIssue) return setFieldError(t(FIELD_ERROR_KEY[clientIssue]));
    submitting.current = true;
    setSaving(true);
    try {
      const outcome = editing
        ? await call(
            () => timeOffUpdate({ path: { time_off_id: editing.id }, body: patchBody({ ...initial, lastDay: initial.allDay ? initial.lastDay : initial.firstDay, tz }, { ...state, lastDay: state.allDay ? state.lastDay : state.firstDay, tz }) }),
            { write: true },
          )
        : await call(() => timeOffCreate({ path: { member_id: memberId }, body: requestBody({ ...state, lastDay: state.allDay ? state.lastDay : state.firstDay, tz }) }), { write: true });
      if ((outcome.status === 200 || outcome.status === 201) && outcome.data) {
        onDone(outcome.data);
        return;
      }
      if (outcome.status === 401) return setSignedOut(true);
      const dayOrTimeCode = outcome.code === "last_day_before_first_day" || outcome.code === "time_off_too_long" || outcome.code === "end_not_after_start";
      if (outcome.status === 422 && dayOrTimeCode) {
        setBannerError(t("notBlocked"));
        if (outcome.code === "last_day_before_first_day") setFieldError(t("errorLastDayBeforeFirst"));
        if (outcome.code === "time_off_too_long") setFieldError(t("errorTooLong"));
        if (outcome.code === "end_not_after_start") setFieldError(t("errorEndNotAfterStart"));
        return;
      }
      setBannerError(`${form(problem(outcome))} ${tConsole("notSaved")}`);
    } catch {
      // A throw here (never expected once `clientProblem` has passed) must still leave the form
      // usable, not stuck on "Sending" forever.
      setBannerError(`${form("unexpected")} ${tConsole("notSaved")}`);
    } finally {
      setSaving(false);
      submitting.current = false;
    }
  }

  async function onRemove() {
    if (!editing || submitting.current) return;
    submitting.current = true;
    setRemoving(true);
    const outcome = await call(() => timeOffDelete({ path: { time_off_id: editing.id }, ...JSON_WRITE }), { write: true });
    setRemoving(false);
    submitting.current = false;
    if (outcome.status === 204) {
      onDone(null);
      return;
    }
    setConfirmingRemove(false);
    if (outcome.status === 401) return setSignedOut(true);
    setBannerError(outcome.status === 404 ? t("notFound") : t("notRemoved"));
  }

  return (
    <>
      <Heading focus>{editing ? t("editTitle") : t("newTitle")}</Heading>
      <p className={uiStyles.lede}>{t("zoneLede", { name: personName, city: zoneCity(settings.timezone) })}</p>
      {signedOut && <SignedOutBanner />}
      {bannerError && <Banner tone="error">{bannerError}</Banner>}
      <form className={`${uiStyles.stack} ${styles.colWide}`} noValidate onSubmit={onSubmit}>
        <label className={styles.choice}>
          <input
            type="checkbox"
            checked={state.allDay}
            disabled={saving}
            onChange={(e) => setState((s) => ({ ...s, allDay: e.target.checked }))}
          />
          <span>
            <b>{t("allDayLabel")}</b>
            <em>{t("allDayHint")}</em>
          </span>
        </label>

        {!state.allDay && wasMultiDayPartial && dateOrTimeTouched && <p className={uiStyles.hint}>{t("multiDayNote")}</p>}

        <div className={styles.row2}>
          <div className={uiStyles.field}>
            <label className={uiStyles.label} htmlFor="bt-first-day">
              {t("firstDayLabel")}
            </label>
            <div className={uiStyles.input}>
              <input
                id="bt-first-day"
                type="date"
                value={state.firstDay}
                disabled={saving}
                onChange={(e) => setState((s) => ({ ...s, firstDay: e.target.value }))}
              />
            </div>
          </div>
          {state.allDay && (
            <div className={uiStyles.field}>
              <label className={uiStyles.label} htmlFor="bt-last-day">
                {t("lastDayLabel")}
              </label>
              <div className={uiStyles.input}>
                <input
                  id="bt-last-day"
                  type="date"
                  value={state.lastDay}
                  disabled={saving}
                  aria-invalid={fieldError ? true : undefined}
                  aria-describedby={fieldError ? "bt-error" : undefined}
                  onChange={(e) => setState((s) => ({ ...s, lastDay: e.target.value }))}
                />
              </div>
              <p className={uiStyles.hint}>{t("lastDayHint")}</p>
            </div>
          )}
        </div>

        {!state.allDay && (
          <div className={styles.row2}>
            <div className={uiStyles.field}>
              <label className={uiStyles.label} htmlFor="bt-start">
                {t("startLabel")}
              </label>
              <div className={uiStyles.input}>
                <input
                  id="bt-start"
                  type="time"
                  step={300}
                  value={state.startTime}
                  disabled={saving}
                  onChange={(e) => setState((s) => ({ ...s, startTime: e.target.value }))}
                />
              </div>
            </div>
            <div className={uiStyles.field}>
              <label className={uiStyles.label} htmlFor="bt-end">
                {t("endLabel")}
              </label>
              <div className={uiStyles.input}>
                <input
                  id="bt-end"
                  type="time"
                  step={300}
                  value={state.endTime}
                  disabled={saving}
                  aria-invalid={fieldError ? true : undefined}
                  aria-describedby={fieldError ? "bt-error" : undefined}
                  onChange={(e) => setState((s) => ({ ...s, endTime: e.target.value }))}
                />
              </div>
            </div>
          </div>
        )}

        {fieldError && <FieldError id="bt-error">{fieldError}</FieldError>}

        <div className={uiStyles.field}>
          <label className={uiStyles.label} htmlFor="bt-reason">
            {t("reasonLabel")} <span className={styles.optional}>· {t("reasonOptional")}</span>
          </label>
          <div className={uiStyles.input}>
            <input
              id="bt-reason"
              type="text"
              maxLength={500}
              autoComplete="off"
              value={state.reason}
              disabled={saving}
              onChange={(e) => setState((s) => ({ ...s, reason: e.target.value }))}
            />
          </div>
          <p className={uiStyles.hint}>{t("reasonHint")}</p>
        </div>

        <div className={styles.actions}>
          <Submit busy={saving} busyLabel={form("sending")}>
            {editing ? t("saveSubmit") : t("blockSubmit")}
          </Submit>
          <button className={`${uiStyles.button} ${uiStyles.secondary} ${styles.block}`} type="button" aria-disabled={saving || undefined} onClick={onCancel}>
            {t("cancel")}
          </button>
        </div>

        {editing && (
          <>
            <hr className={styles.divider} />
            {!confirmingRemove ? (
              <button
                className={`${uiStyles.button} ${styles.danger} ${styles.block}`}
                type="button"
                onClick={() => setConfirmingRemove(true)}
              >
                {t("removeButton")}
              </button>
            ) : (
              <div className={uiStyles.stack}>
                <p>{t("removeConfirmTitle")}</p>
                <div className={styles.actions}>
                  <button
                    ref={confirmRef}
                    className={`${uiStyles.button} ${styles.danger} ${styles.block}`}
                    type="button"
                    aria-disabled={removing || undefined}
                    onClick={onRemove}
                  >
                    {removing ? form("sending") : t("removeConfirm")}
                  </button>
                  <button
                    className={`${uiStyles.button} ${uiStyles.secondary} ${styles.block}`}
                    type="button"
                    disabled={removing}
                    onClick={() => setConfirmingRemove(false)}
                  >
                    {t("cancel")}
                  </button>
                </div>
              </div>
            )}
            <p className={uiStyles.hint}>{t("removeHint")}</p>
          </>
        )}
      </form>
    </>
  );
}
