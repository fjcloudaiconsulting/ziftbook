"use client";

import { useLocale, useTranslations } from "next-intl";
import { type FormEvent, type ReactNode, useEffect, useId, useRef, useState } from "react";

import { type BusinessSettingsOutput, settingsUpdate } from "@/api-client";
import { bookingPageAddress, dateLocale } from "@/lib/console";
import {
  breakMinutes,
  changedKeys,
  type Draft,
  draftFrom,
  effective,
  FIELDS,
  type Key,
  mergeSaved,
  noticeOptions,
  noticeUnit,
  pickFields,
  SLOT_STEPS,
  settingsBody,
  slotExamples,
  zoneOptions,
} from "@/lib/settings";

import { SignedOutBanner, useConsole } from "../../_ui/console";
import styles from "../../_ui/console.module.css";
import { NAMES } from "../../_ui/header";
import { Banner, FieldError, Heading, problem, Submit } from "../../_ui/parts";
import { SaveBar } from "../../_ui/save-bar";
import uiStyles from "../../_ui/ui.module.css";

const bare = { border: 0, margin: 0, minWidth: 0 } as const;

/** Label, control, then unit, error and hint, all tied to the control with aria-describedby. */
function Field({
  id,
  label,
  unit,
  hint,
  error,
  short,
  children,
}: {
  id: string;
  label: string;
  unit?: string;
  hint?: string;
  error?: string | false;
  short?: boolean;
  children: (p: { id: string; describedBy: string | undefined; invalid: true | undefined }) => ReactNode;
}) {
  const ids = [unit && `${id}-unit`, error ? `${id}-error` : hint && `${id}-hint`].filter(Boolean);
  const control = children({ id, describedBy: ids.join(" ") || undefined, invalid: error ? true : undefined });
  return (
    <div className={uiStyles.field}>
      <label className={uiStyles.label} htmlFor={id}>
        {label}
      </label>
      {unit ? (
        <div style={{ display: "flex", alignItems: "center", gap: "0.6rem" }}>
          <div className={uiStyles.input} style={{ width: "7rem" }}>
            {control}
          </div>
          <span className={uiStyles.hint} id={`${id}-unit`}>
            {unit}
          </span>
        </div>
      ) : (
        <div className={uiStyles.input} style={short ? { maxWidth: "12rem" } : undefined}>
          {control}
        </div>
      )}
      {error ? (
        <FieldError id={`${id}-error`}>{error}</FieldError>
      ) : (
        hint && (
          <p className={uiStyles.hint} id={`${id}-hint`}>
            {hint}
          </p>
        )
      )}
    </div>
  );
}

function Chevron() {
  return (
    <svg aria-hidden="true" className={uiStyles.selectChevron} viewBox="0 0 12 12" width="12" height="12">
      <path d="M2 4.5l4 4 4-4" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function Settings() {
  const { session, settings, call, updateSettings } = useConsole();
  const t = useTranslations("Console.settings");
  const tWeek = useTranslations("Console.week");
  const nav = useTranslations("Console.nav");
  const form = useTranslations("Form");
  const errorsT = useTranslations("Console.errors");
  const locale = useLocale();
  const formId = useId();
  const [draft, setDraft] = useState<Draft>(() => draftFrom(settings));
  const [invalid, setInvalid] = useState<Key[]>([]);
  const [saving, setSaving] = useState(false);
  const [saveFailed, setSaveFailed] = useState(false);
  const [message, setMessage] = useState<{ tone: "error" | "info"; text: string } | null>(null);
  const [focusMessage, setFocusMessage] = useState(0);
  const [signedOut, setSignedOut] = useState(false);
  const submitting = useRef(false);
  const messageRef = useRef<HTMLDivElement>(null);
  const topRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (focusMessage) messageRef.current?.focus();
  }, [focusMessage]);

  // Safe to read during render: the shell mounts its children only once the session and settings
  // fetch has already resolved (console.tsx), so there is no server/client mismatch to guard with
  // a useEffect here.
  const origin = window.location.origin;
  const address = bookingPageAddress(origin, session.slug);
  const [publishing, setPublishing] = useState(false);
  const publishingRef = useRef(false);
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const addressRef = useRef<HTMLSpanElement>(null);

  const changed = changedKeys(settings, draft);
  const dirty = changed.length > 0;
  const list = (keys: Key[]) => new Intl.ListFormat(locale, { type: "unit" }).format(keys.map((k) => t(`changed.${k}`)));
  const unit = (n: number, u: string) => new Intl.NumberFormat(locale, { style: "unit", unit: u, unitDisplay: "long" }).format(n);
  const minutes = (n: number) => unit(n, "minute");
  const time = (m: number) =>
    new Intl.DateTimeFormat(dateLocale(locale), { hour: "numeric", minute: "2-digit", timeZone: "UTC" }).format(new Date(Date.UTC(2000, 0, 1, 0, m)));
  const noticeLabel = (n: number) => {
    const u = noticeUnit(n);
    return u.unit === "none" ? t("noticeNone") : unit(u.n, u.unit);
  };
  const rangeMsg = (key: Key) => {
    const f = FIELDS.find((x) => x.key === key)!;
    return t("wholeNumber", { min: f.min!, max: f.max! });
  };
  const set = (key: Key, value: string | boolean) => setDraft((d) => ({ ...d, [key]: value }));
  const err = (key: Key) => invalid.includes(key) && rangeMsg(key);
  const step = effective(settings, draft, "slot_step_minutes");
  const pct = effective(settings, draft, "buffer_pct");

  async function onTogglePublish() {
    if (publishingRef.current) return;
    const next = !settings.published;
    publishingRef.current = true;
    setPublishing(true);
    setMessage(null);
    setSignedOut(false);
    const outcome = await call(() => settingsUpdate({ body: { published: next } }), { write: true });
    setPublishing(false);
    publishingRef.current = false;
    if (outcome.status === 200 && outcome.data) {
      // Only `published`: a late response must not roll back keys saved since it was sent.
      updateSettings({ published: outcome.data.published });
      setMessage(next ? { tone: "info", text: t("publishedBanner") } : null);
    } else if (outcome.status === 401) {
      setSignedOut(true);
    } else {
      setMessage({ tone: "error", text: form(problem(outcome)) });
    }
  }

  async function onCopy() {
    // Empty the live region first: a second "Copied" into an unchanged region isn't announced.
    setCopyState("idle");
    try {
      if (!navigator.clipboard) throw new Error("no clipboard");
      await navigator.clipboard.writeText(address);
      setCopyState("copied");
    } catch {
      const node = addressRef.current;
      const range = node && document.createRange();
      if (range) {
        range.selectNodeContents(node);
        const selection = window.getSelection();
        selection?.removeAllRanges();
        selection?.addRange(range);
      }
      setCopyState("failed");
    }
  }

  function undo() {
    setDraft(draftFrom(settings));
    setInvalid([]);
    setSaveFailed(false);
    setMessage(null);
    setSignedOut(false);
    topRef.current?.querySelector("h1")?.focus();
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (submitting.current) return;
    const { body, invalid: bad } = settingsBody(settings, draft);
    setMessage(null);
    setSignedOut(false);
    setSaveFailed(false);
    setInvalid(bad);
    if (bad.length > 0) {
      document.getElementById(`${formId}-${bad[0]}`)?.focus();
      return;
    }
    const sent = Object.keys(body) as Key[];
    if (sent.length === 0) return;
    submitting.current = true;
    setSaving(true);
    try {
      let outcome;
      try {
        outcome = await call(() => settingsUpdate({ body: body as Parameters<typeof settingsUpdate>[0]["body"] }), { write: true });
      } catch {
        outcome = { status: 0 } as Awaited<ReturnType<typeof call<BusinessSettingsOutput>>>;
      }
      if (outcome.status === 200 && outcome.data) {
        const data = outcome.data;
        // Only the form's own keys: never `published`, which the switch above owns.
        updateSettings(pickFields(data) as Partial<BusinessSettingsOutput>);
        setDraft((d) => mergeSaved(d, data, sent));
        setMessage({ tone: "info", text: t("savedList", { list: list(sent) }) });
        setFocusMessage((n) => n + 1);
      } else if (outcome.status === 401) {
        setSignedOut(true);
        setSaveFailed(true);
      } else if (outcome.status === 403 && outcome.code === "owner_only") {
        setMessage({ tone: "error", text: errorsT("ownerOnly") });
        setSaveFailed(true);
      } else {
        setMessage({ tone: "error", text: form(problem(outcome)) });
        setSaveFailed(true);
      }
    } finally {
      setSaving(false);
      submitting.current = false;
    }
  }

  const int = (key: Key, id: string, describedBy: string | undefined, invalidAttr: true | undefined) => (
    <input
      id={id}
      type="text"
      inputMode="numeric"
      value={String(draft[key])}
      onChange={(e) => set(key, e.target.value)}
      aria-invalid={invalidAttr}
      aria-describedby={describedBy}
    />
  );
  const pick = (key: Key, id: string, describedBy: string | undefined, options: { value: string; label: string }[]) => (
    <>
      <select id={id} value={String(draft[key])} onChange={(e) => set(key, e.target.value)} aria-describedby={describedBy}>
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
      <Chevron />
    </>
  );
  const fid = (key: Key) => `${formId}-${key}`;
  const zones = zoneOptions(settings.timezone, Intl.supportedValuesOf("timeZone"));
  const showBar = dirty || saving || saveFailed;

  return (
    <>
      <div ref={topRef}>
        <Heading focus>{nav("settings")}</Heading>
      </div>
      {signedOut && <SignedOutBanner />}
      {message && (
        <div ref={messageRef} tabIndex={-1}>
          <Banner tone={message.tone}>{message.text}</Banner>
        </div>
      )}
      <div className={styles.section}>
        <h2>{t("bookingPageHeading")}</h2>
        <span className={`${styles.statusPill} ${settings.published ? styles.statusLive : ""}`}>
          {settings.published ? (
            <svg aria-hidden="true" viewBox="0 0 16 16">
              <circle cx="8" cy="8" r="6.5" fill="currentColor" />
              <path d="M5 8.2l2 2 4-4.3" fill="none" stroke="white" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          ) : (
            <svg aria-hidden="true" viewBox="0 0 16 16">
              <circle cx="8" cy="8" r="5.5" fill="none" stroke="currentColor" strokeWidth="1.6" />
            </svg>
          )}
          {t(settings.published ? "statusPublished" : "statusNotPublished")}
        </span>
        <p className={styles.body}>{t(settings.published ? "publishedBody" : "unpublishedBody")}</p>
        <div className={styles.address}>
          <span className={uiStyles.label}>{t("addressLabel")}</span>
          <div className={`${styles.url} ${settings.published ? "" : styles.urlOff}`}>
            <span ref={addressRef}>
              {origin}
              {"/"}
              <wbr />
              <b className={styles.urlSlug}>{session.slug}</b>
            </span>
            <button className={styles.copyButton} type="button" onClick={onCopy}>
              {t("copy")}
            </button>
          </div>
          <p className={uiStyles.srOnly} role="status" aria-live="polite">
            {copyState === "copied" ? t("copied") : copyState === "failed" ? t("copyFailed") : ""}
          </p>
        </div>
        {settings.published && (
          <p className={styles.row}>
            <a className={uiStyles.textButton} href={address} target="_blank" rel="noopener">
              {t("openPage")}
              <svg aria-hidden="true" viewBox="0 0 16 16" width="14" height="14">
                <path d="M4 12l8-8M7 4h5v5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
              <span className={uiStyles.srOnly}>{t("opensInNewTab")}</span>
            </a>
          </p>
        )}
        <form
          noValidate
          onSubmit={(event) => {
            event.preventDefault();
            onTogglePublish();
          }}
        >
          <Submit busy={publishing} busyLabel={form("sending")} secondary={settings.published}>
            {t(settings.published ? "unpublish" : "publish")}
          </Submit>
        </form>
        {settings.published && <p className={uiStyles.hint}>{t("unpublishHint")}</p>}
      </div>

      <form id={formId} noValidate onSubmit={onSubmit} className={uiStyles.stack} style={{ gap: 0 }}>
        <fieldset className={styles.section} style={bare} disabled={saving}>
          <legend style={{ padding: 0, marginBottom: "0.9rem" }}>
            <h2 style={{ margin: 0 }}>{t("yourBusinessHeading")}</h2>
          </legend>
          <Field id={fid("timezone")} label={t("timezoneLabel")} hint={t("timezoneHint")}>
            {(p) => pick("timezone", p.id, p.describedBy, zones.map((z) => ({ value: z, label: z })))}
          </Field>
          <Field id={fid("language")} label={t("languageLabel")} hint={t("languageHint")} short>
            {(p) => pick("language", p.id, p.describedBy, Object.entries(NAMES).map(([value, label]) => ({ value, label })))}
          </Field>
        </fieldset>

        <fieldset className={styles.section} style={{ ...bare, marginTop: "1.25rem" }} disabled={saving}>
          <legend style={{ padding: 0, marginBottom: "0.9rem" }}>
            <h2 style={{ margin: 0 }}>{t("bookingsHeading")}</h2>
          </legend>
          <div style={{ display: "flex", gap: "0.75rem", alignItems: "flex-start" }}>
            <input
              id={fid("auto_confirm")}
              type="checkbox"
              checked={draft.auto_confirm as boolean}
              onChange={(e) => set("auto_confirm", e.target.checked)}
              aria-describedby={`${fid("auto_confirm")}-hint`}
              style={{ width: "1.5rem", height: "1.5rem", margin: "0.1rem 0 0", accentColor: "var(--primary)", flex: "none" }}
            />
            <div className={uiStyles.field} style={{ gap: "0.15rem" }}>
              <label className={uiStyles.label} htmlFor={fid("auto_confirm")}>
                {t("autoConfirmLabel")}
              </label>
              <p className={uiStyles.hint} id={`${fid("auto_confirm")}-hint`}>
                {t("autoConfirmHint")}
              </p>
            </div>
          </div>
          <Field
            id={fid("slot_step_minutes")}
            label={t("slotStepLabel")}
            hint={t("slotStepHint", { step: minutes(step), examples: slotExamples(step).map(time).join(", ") })}
            short
          >
            {(p) => pick("slot_step_minutes", p.id, p.describedBy, SLOT_STEPS.map((n) => ({ value: String(n), label: minutes(n) })))}
          </Field>
          <Field id={fid("min_notice_minutes")} label={t("minNoticeLabel")} hint={t("minNoticeHint")} short>
            {(p) =>
              pick(
                "min_notice_minutes",
                p.id,
                p.describedBy,
                noticeOptions(settings.min_notice_minutes).map((n) => ({ value: String(n), label: noticeLabel(n) })),
              )
            }
          </Field>
          <Field
            id={fid("booking_horizon_days")}
            label={t("horizonLabel")}
            unit={t("daysUnit", { count: Number(draft.booking_horizon_days) || settings.booking_horizon_days })}
            error={err("booking_horizon_days")}
          >
            {(p) => int("booking_horizon_days", p.id, p.describedBy, p.invalid)}
          </Field>
          <Field
            id={fid("buffer_pct")}
            label={t("bufferLabel")}
            unit={t("percentUnit", { count: pct })}
            hint={t("bufferHint", { minutes: minutes(breakMinutes(pct)) })}
            error={err("buffer_pct")}
          >
            {(p) => int("buffer_pct", p.id, p.describedBy, p.invalid)}
          </Field>
          <Field
            id={fid("pending_ttl_hours")}
            label={t("ttlLabel")}
            unit={t("hoursUnit", { count: Number(draft.pending_ttl_hours) || settings.pending_ttl_hours })}
            hint={t("ttlHint")}
            error={err("pending_ttl_hours")}
          >
            {(p) => int("pending_ttl_hours", p.id, p.describedBy, p.invalid)}
          </Field>
          <Field
            id={fid("max_pending_per_email")}
            label={t("pendingLabel")}
            unit={t("atATimeUnit", { count: Number(draft.max_pending_per_email) || settings.max_pending_per_email })}
            error={err("max_pending_per_email")}
          >
            {(p) => int("max_pending_per_email", p.id, p.describedBy, p.invalid)}
          </Field>
        </fieldset>

        <fieldset className={styles.section} style={{ ...bare, marginTop: "1.25rem", borderBottom: 0 }} disabled={saving}>
          <legend style={{ padding: 0, marginBottom: "0.9rem" }}>
            <h2 style={{ margin: 0 }}>{t("cancellingHeading")}</h2>
          </legend>
          <Field
            id={fid("max_reschedules")}
            label={t("maxReschedulesLabel")}
            unit={t("timesUnit", { count: Number(draft.max_reschedules) || 0 })}
            hint={t("maxReschedulesHint")}
            error={err("max_reschedules")}
          >
            {(p) => int("max_reschedules", p.id, p.describedBy, p.invalid)}
          </Field>
        </fieldset>

        {showBar && (
          <SaveBar
            formId={formId}
            hint={saving ? tWeek("savingHint") : dirty ? t("unsavedChanges", { list: list(changed) }) : ""}
            busy={saving}
            onUndo={saving ? undefined : undo}
            saveLabel={t("saveChanges")}
            savingLabel={tWeek("saving")}
            undoLabel={tWeek("undo")}
          />
        )}
      </form>
    </>
  );
}
