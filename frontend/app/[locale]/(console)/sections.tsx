"use client";

import { useLocale, useTranslations } from "next-intl";
import { type FormEvent, useId, useRef, useState } from "react";

import { settingsUpdate } from "@/api-client";
import { Link } from "@/i18n/navigation";
import { bookingPageAddress, canEditSettings, maxReschedulesValue, type Role, showPublishStep, todayLabel } from "@/lib/console";

import { SignedOutBanner, useConsole } from "../_ui/console";
import styles from "../_ui/console.module.css";
import { Banner, FieldError, Heading, Mark, problem, Submit } from "../_ui/parts";
import uiStyles from "../_ui/ui.module.css";

export function Today() {
  const { session, settings } = useConsole();
  const t = useTranslations("Console.today");
  const nav = useTranslations("Console.nav");
  const locale = useLocale();
  const label = todayLabel(new Date(), settings.timezone, locale);

  if (session.role === "worker") {
    return (
      <>
        <Heading focus>{nav("today")}</Heading>
        <p className={uiStyles.lede}>{label}</p>
        <div className={uiStyles.empty}>
          <Mark icon="calendar" />
          <strong>{t("workerEmptyTitle")}</strong>
          <span>{t("workerEmptyBody")}</span>
          <Link className={`${uiStyles.button} ${uiStyles.secondary}`} href="/my-hours">
            {t("checkHours")}
          </Link>
        </div>
      </>
    );
  }

  return (
    <>
      <Heading focus>{nav("today")}</Heading>
      <p className={uiStyles.lede}>{label}</p>
      <div className={uiStyles.empty}>
        <Mark icon="calendar" />
        <strong>{t("ownerEmptyTitle")}</strong>
        <span>{t("ownerEmptyBody")}</span>
      </div>
      <ol className={styles.steps} aria-label={t("steps")}>
        <li>
          <span className={styles.stepMark} aria-hidden="true">
            {1}
          </span>
          <Link className={uiStyles.textButton} href="/opening-hours">
            {t("step1")}
          </Link>
        </li>
        <li>
          <span className={styles.stepMark} aria-hidden="true">
            {2}
          </span>
          <Link className={uiStyles.textButton} href="/services">
            {t("step2")}
          </Link>
        </li>
        <li>
          <span className={styles.stepMark} aria-hidden="true">
            {3}
          </span>
          <Link className={uiStyles.textButton} href={`/team/${session.member_id}`}>
            {t("step3")}
          </Link>
        </li>
        <li>
          <span className={styles.stepMark} aria-hidden="true">
            {4}
          </span>
          <Link className={uiStyles.textButton} href="/team">
            {t("step4")}
          </Link>
        </li>
        {showPublishStep(settings.published) && (
          <li>
            <span className={styles.stepMark} aria-hidden="true">
              {5}
            </span>
            <Link className={uiStyles.textButton} href="/settings">
              {t("step5")}
            </Link>
          </li>
        )}
      </ol>
      <p className={styles.stepsHint}>{t("stepsHint")}</p>
    </>
  );
}

export function Calendar() {
  const { session } = useConsole();
  const t = useTranslations("Console.calendar");
  const nav = useTranslations("Console.nav");
  return (
    <>
      <Heading focus>{nav("calendar")}</Heading>
      <div className={styles.placeholder}>
        <p>{t("comingSoon")}</p>
        {session.role === "owner" ? (
          <Link className={`${uiStyles.button} ${uiStyles.secondary} ${styles.placeholderAction}`} href={`/team/${session.member_id}`}>
            {t("seeHours")}
          </Link>
        ) : (
          <Link className={`${uiStyles.button} ${uiStyles.secondary} ${styles.placeholderAction}`} href="/my-hours">
            {t("seeMyHours")}
          </Link>
        )}
      </div>
    </>
  );
}

export function Clients() {
  const t = useTranslations("Console.clients");
  const nav = useTranslations("Console.nav");
  return (
    <>
      <Heading focus>{nav("clients")}</Heading>
      <div className={styles.placeholder}>
        <p>{t("comingSoon")}</p>
      </div>
    </>
  );
}

export function Settings() {
  const { session, settings, call, updateSettings } = useConsole();
  const t = useTranslations("Console.settings");
  const nav = useTranslations("Console.nav");
  const form = useTranslations("Form");
  const id = useId();
  const editable = canEditSettings(session.role as Role);
  const [value, setValue] = useState(String(settings.max_reschedules));
  const [saving, setSaving] = useState(false);
  const [fieldError, setFieldError] = useState(false);
  const [message, setMessage] = useState<{ tone: "error" | "info"; text: string } | null>(null);
  const [signedOut, setSignedOut] = useState(false);
  const submitting = useRef(false);

  // Safe to read during render: the shell mounts its children only once the session and settings
  // fetch has already resolved (console.tsx), so there is no server/client mismatch to guard with
  // a useEffect here.
  const origin = window.location.origin;
  const address = bookingPageAddress(origin, session.slug);
  const [publishing, setPublishing] = useState(false);
  const publishingRef = useRef(false);
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const addressRef = useRef<HTMLSpanElement>(null);

  async function onTogglePublish() {
    if (publishingRef.current || !editable) return;
    const next = !settings.published;
    publishingRef.current = true;
    setPublishing(true);
    setMessage(null);
    setSignedOut(false);
    const outcome = await call(() => settingsUpdate({ body: { published: next } }), { write: true });
    setPublishing(false);
    publishingRef.current = false;
    if (outcome.status === 200 && outcome.data) {
      updateSettings(outcome.data);
      setMessage(next ? { tone: "info", text: t("publishedBanner") } : null);
    } else if (outcome.status === 401) {
      setSignedOut(true);
    } else {
      setMessage({ tone: "error", text: form(problem(outcome)) });
    }
  }

  async function onCopy() {
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

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (submitting.current || !editable) return;
    const max = maxReschedulesValue(value);
    setFieldError(max === null);
    setMessage(null);
    setSignedOut(false);
    if (max === null) return;
    submitting.current = true;
    setSaving(true);
    const outcome = await call(() => settingsUpdate({ body: { max_reschedules: max } }), { write: true });
    setSaving(false);
    submitting.current = false;
    if (outcome.status === 200 && outcome.data) {
      setValue(String(outcome.data.max_reschedules));
      setMessage({ tone: "info", text: t("saved") });
    } else if (outcome.status === 401) {
      setSignedOut(true);
    } else {
      setMessage({ tone: "error", text: form(problem(outcome)) });
    }
  }

  return (
    <>
      <Heading focus>{nav("settings")}</Heading>
      {signedOut && <SignedOutBanner />}
      {message && <Banner tone={message.tone}>{message.text}</Banner>}
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
        {editable && <p className={styles.body}>{t(settings.published ? "publishedBody" : "unpublishedBody")}</p>}
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
        {editable && settings.published && (
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
        {editable ? (
          <>
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
          </>
        ) : (
          <p className={uiStyles.hint}>{t("workerHint")}</p>
        )}
      </div>
      <form className={`${uiStyles.form} ${uiStyles.col}`} noValidate onSubmit={onSubmit}>
        <div className={uiStyles.field}>
          <label className={uiStyles.label} htmlFor={id}>
            {t("maxReschedulesLabel")}
          </label>
          <div className={uiStyles.input}>
            <input
              id={id}
              type="number"
              inputMode="numeric"
              min={0}
              max={10}
              step={1}
              value={value}
              readOnly={!editable}
              onChange={(event) => setValue(event.target.value)}
              aria-invalid={fieldError ? true : undefined}
              aria-describedby={fieldError ? `${id}-error` : `${id}-hint`}
            />
          </div>
          {fieldError ? (
            <FieldError id={`${id}-error`}>{t("maxReschedulesRange")}</FieldError>
          ) : (
            <p className={uiStyles.hint} id={`${id}-hint`}>
              {t("maxReschedulesHint")} {!editable && t("maxReschedulesOwnerOnly")}
            </p>
          )}
        </div>
        {editable && (
          <Submit busy={saving} busyLabel={form("sending")}>
            {t("save")}
          </Submit>
        )}
      </form>
    </>
  );
}
