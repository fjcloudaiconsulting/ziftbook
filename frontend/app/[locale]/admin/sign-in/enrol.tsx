"use client";

import { useTranslations } from "next-intl";
import { type FormEvent, useEffect, useId, useRef, useState } from "react";

import { adminBeginEnrolment, adminConfirmEnrolment, type AdminSession, type EnrolmentOut } from "@/api-client";
import { appCode, factorState, recoveryFile, secretGroups } from "@/lib/admin";

import { Banner, FieldError, Heading, Mark, problem, send, Submit } from "../../_ui/parts";
import styles from "../../_ui/ui.module.css";
import admin from "../admin.module.css";
import { CodeField, CopyButton, EnrolCommand } from "../parts";

type Message = { tone: "error" | "note"; text: string };

/** No confirmed factor yet: a ticket from the operator's CLI starts enrolment (ZIF-71 T1). */
export function TicketStep({
  who,
  message: initial,
  initialTicket,
  focus,
  onBegun,
  onEnrolled,
  onExpired,
}: {
  who: AdminSession;
  message?: Message;
  initialTicket?: string;
  focus: boolean;
  onBegun(enrolment: EnrolmentOut): void;
  onEnrolled(): void;
  /** `ticket`: what was typed, so a sign-in after the enrol command's sign-out needn't retype it. */
  onExpired(ticket: string): void;
}) {
  const t = useTranslations("Admin");
  const form = useTranslations("Form");
  const id = useId();
  const input = useRef<HTMLInputElement>(null);
  const [ticket, setTicket] = useState(initialTicket ?? "");
  const [busy, setBusy] = useState(false);
  const [wrong, setWrong] = useState(false);
  const [message, setMessage] = useState<Message | null>(initial ?? null);
  // The parent passes a new message object per step change, e.g. after a 409 whose follow-up read failed.
  const [seen, setSeen] = useState(initial);
  if (initial !== seen) {
    setSeen(initial);
    if (initial) setMessage(initial);
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    const outcome = await send(adminBeginEnrolment({ body: { ticket: ticket.trim() } }));
    const state = factorState(outcome.status, outcome.code);
    setBusy(false);
    setWrong(false);
    setMessage(null);

    if (outcome.status === 200 && outcome.data) {
      onBegun(outcome.data);
    } else if (state === "wrong") {
      setWrong(true);
      input.current?.focus();
    } else if (state === "expired") {
      onExpired(ticket.trim());
    } else if (state === "conflict") {
      onEnrolled();
    } else if (state === "tooMany") {
      setMessage({ tone: "note", text: t("tooMany") });
    } else if (state === "unavailable") {
      setMessage({ tone: "note", text: t("unavailable") });
    } else {
      setMessage({ tone: outcome.status === 503 ? "note" : "error", text: form(problem(outcome)) });
    }
  }

  return (
    <>
      <Mark icon="key" />
      <Heading focus={focus}>{t("enrolTitle")}</Heading>
      <p className={styles.lede}>{t("enrolLede")}</p>
      {message && <Banner tone={message.tone}>{message.text}</Banner>}
      <div className={admin.section}>
        <p className={styles.hint}>{t("commandIntro")}</p>
        <EnrolCommand email={who.email} />
      </div>
      <form className={styles.form} method="post" onSubmit={onSubmit}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={id}>
            {t("ticket")}
          </label>
          <div className={`${styles.input} ${admin.recovery}`}>
            <input
              ref={input}
              id={id}
              name="ticket"
              className={admin.mono}
              autoComplete="off"
              autoCapitalize="characters"
              spellCheck={false}
              placeholder="XXXX-XXXX-XXXX-XXXX"
              maxLength={40}
              required
              value={ticket}
              onChange={(event) => setTicket(event.target.value)}
              aria-invalid={wrong || undefined}
              aria-describedby={`${id}-hint`}
            />
          </div>
          {wrong ? (
            <FieldError id={`${id}-hint`}>{t("wrongTicket")}</FieldError>
          ) : (
            <p className={styles.hint} id={`${id}-hint`}>
              {t("ticketHint")}
            </p>
          )}
        </div>
        <Submit busy={busy} busyLabel={t("checking")}>
          {t("continue")}
        </Submit>
      </form>
    </>
  );
}

/** The QR, the same secret as a typed key, and the first code that proves the app has it. */
export function ScanStep({
  who,
  enrolment,
  onEnrolled,
  onRestart,
  onExpired,
}: {
  who: AdminSession;
  enrolment: EnrolmentOut;
  onEnrolled(codes: string[]): void;
  onRestart(message?: Message): void;
  onExpired(): void;
}) {
  const t = useTranslations("Admin");
  const form = useTranslations("Form");
  const input = useRef<HTMLInputElement>(null);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [locked, setLocked] = useState(false);
  const [message, setMessage] = useState<Message | null>(null);
  const groups = secretGroups(enrolment.secret);
  const keyText = useRef<HTMLParagraphElement>(null);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    const value = appCode(code);
    if (!value) {
      setError(t("codeFormat"));
      input.current?.focus();
      return;
    }
    setBusy(true);
    const outcome = await send(adminConfirmEnrolment({ body: { code: value } }));
    const state = factorState(outcome.status, outcome.code);
    setBusy(false);
    setError(undefined);
    setMessage(null);

    if (outcome.status === 200 && outcome.data) {
      onEnrolled(outcome.data.recovery_codes);
    } else if (state === "wrong") {
      setError(t("wrongCode"));
      setCode("");
      input.current?.focus();
    } else if (state === "expired") {
      onExpired();
    } else if (state === "locked") {
      setLocked(true);
    } else if (state === "conflict") {
      onRestart({ tone: "note", text: t("ticketExpired") });
    } else if (state === "tooMany") {
      setMessage({ tone: "note", text: t("tooMany") });
    } else if (state === "unavailable") {
      setMessage({ tone: "note", text: t("unavailable") });
    } else {
      setMessage({ tone: outcome.status === 503 ? "note" : "error", text: form(problem(outcome)) });
    }
  }

  if (locked) {
    return (
      <>
        <Mark icon="key" />
        <Heading focus>{t("enrolLockedTitle")}</Heading>
        <Banner tone="error">{t("enrolLocked")}</Banner>
        <EnrolCommand email={who.email} />
        <button className={`${styles.button} ${styles.secondary}`} type="button" onClick={() => onRestart()}>
          {t("newTicket")}
        </button>
      </>
    );
  }

  return (
    <>
      <Heading focus>{t("scanTitle")}</Heading>
      <p className={styles.lede}>{t("scanLede")}</p>
      <figure className={admin.qr}>
        {/* A data: URI from our own API (segno SVG), so next/image adds nothing here. */}
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={enrolment.qr} alt={t("qrAlt")} width={200} height={200} />
      </figure>
      <a className={`${styles.button} ${styles.secondary} ${admin.onPhone}`} href={enrolment.otpauth_uri}>
        {t("openInApp")}
      </a>
      <div className={admin.section}>
        <p className={styles.hint}>{t("manualKey")}</p>
        <div className={admin.keyRow}>
          <p ref={keyText} className={`${admin.key} ${admin.mono}`}>
            {groups.map((group, index) => (
              <span key={index}>{group}</span>
            ))}
          </p>
          <CopyButton text={enrolment.secret} label={t("copyKey")} target={keyText} />
        </div>
      </div>
      <div className={styles.divider} />
      {message && <Banner tone={message.tone}>{message.text}</Banner>}
      <form className={styles.form} method="post" onSubmit={onSubmit} noValidate>
        <CodeField kind="app" label={t("confirmCode")} value={code} onChange={setCode} error={error} inputRef={input} />
        <Submit busy={busy} busyLabel={t("confirming")}>
          {t("confirm")}
        </Submit>
      </form>
    </>
  );
}

/** Shown once: the server keeps only their hashes, so leaving this screen loses them for good. */
export function RecoveryCodes({ who, codes, onDone }: { who: AdminSession; codes: string[]; onDone(): void }) {
  const t = useTranslations("Admin");
  const checkId = useId();
  const [saved, setSaved] = useState(false);
  const codesList = useRef<HTMLOListElement>(null);
  const [nudge, setNudge] = useState(false);
  const text = recoveryFile(t("codesFile", { email: who.email }), codes);

  // Closing the tab or reloading before they're saved asks first.
  useEffect(() => {
    if (saved) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [saved]);

  function download() {
    const url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
    const link = Object.assign(document.createElement("a"), { href: url, download: "ziftbook-recovery-codes.txt" });
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000); // not before the browser has started the download
  }

  function onContinue() {
    if (saved) onDone();
    else setNudge(true);
  }

  return (
    <>
      <Mark icon="done" />
      <Heading focus>{t("codesTitle")}</Heading>
      <p className={styles.lede}>{t("codesLede")}</p>
      <div className={admin.once} role="note">
        <svg aria-hidden="true" viewBox="0 0 16 16" width="18" height="18">
          <circle cx="8" cy="8" r="6.5" fill="none" stroke="currentColor" strokeWidth="1.6" />
          <path d="M8 4.5v4M8 11h.01" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
        </svg>
        <strong>{t("codesOnce")}</strong>
        <p>{t("codesOnceDetail")}</p>
      </div>
      <ol ref={codesList} className={`${admin.codes} ${admin.mono}`} aria-label={t("codesLabel")}>
        {codes.map((code) => (
          <li key={code}>{code}</li>
        ))}
      </ol>
      <div className={admin.buttons}>
        <CopyButton text={text} label={t("copyCodes")} target={codesList} />
        <button className={`${styles.button} ${styles.secondary} ${styles.small}`} type="button" onClick={download}>
          {t("download")}
        </button>
      </div>
      <div className={styles.divider} />
      <div className={styles.field}>
        <label className={admin.check} htmlFor={checkId}>
          <input
            id={checkId}
            type="checkbox"
            checked={saved}
            onChange={(event) => {
              setSaved(event.target.checked);
              setNudge(false);
            }}
            aria-invalid={nudge || undefined}
            aria-describedby={nudge ? `${checkId}-error` : undefined}
          />
          {t("saved")}
        </label>
        {nudge && <FieldError id={`${checkId}-error`}>{t("savedFirst")}</FieldError>}
      </div>
      <button className={`${styles.button} ${styles.primary}`} type="button" onClick={onContinue}>
        {t("done")}
      </button>
    </>
  );
}
