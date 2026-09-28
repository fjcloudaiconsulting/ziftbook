"use client";

import { useLocale, useTranslations } from "next-intl";
import { type FormEvent, useId, useRef, useState } from "react";

import { settingsUpdate } from "@/api-client";
import { Link } from "@/i18n/navigation";
import { canEditSettings, maxReschedulesValue, type Role, todayLabel } from "@/lib/console";

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
  const { session, settings, call } = useConsole();
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
