"use client";

import { useTranslations } from "next-intl";
import { type FormEvent, useEffect, useId, useRef, useState } from "react";

import { adminReactivate, adminSuspend, type BusinessDetail } from "@/api-client";

import { Banner, FieldError, problem, send } from "../../../_ui/parts";
import styles from "../../../_ui/ui.module.css";
import { useSecondFactorRecheck } from "../../recheck";

export type Done = { tone: "info" | "note"; text: string };
type Message = { tone: "error" | "note"; text: string };

/** ZIF-73: suspend or reactivate this business, with a reason, in place at the foot of the detail. A stale
 * second factor (writes need one from the last 15 minutes) swaps the reason form for a code form, keeps the
 * reason and sends the write again once the code passes (useSecondFactorRecheck). */
export function Suspension({ business, onOpen, onChanged }: { business: BusinessDetail; onOpen(): void; onChanged(done: Done): void }) {
  const t = useTranslations("Admin");
  const form = useTranslations("Form");
  const suspended = business.suspended_at !== null;
  const titleId = useId();
  const reasonId = useId();
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [reasonError, setReasonError] = useState<string>();
  const [message, setMessage] = useState<Message | null>(null);
  const opener = useRef<HTMLButtonElement>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);

  const keep = t(suspended ? "keepSuspended" : "keepRunning");
  const recheck = useSecondFactorRecheck({
    titleId,
    cancelLabel: keep,
    onCancel: cancel,
    attempt() {
      setMessage(null);
      setReasonError(undefined);
      return send((suspended ? adminReactivate : adminSuspend)({ path: { tenant_id: business.id }, body: { reason: reason.trim() } }));
    },
    onStep(step, outcome) {
      if (step === "done") {
        setOpen(false);
        onChanged({ tone: "info", text: t(suspended ? "reactivatedDone" : "suspendedDone") });
      } else if (step === "changed") {
        setOpen(false);
        onChanged({ tone: "note", text: t("changedNote") });
      } else if (step === "notFound") {
        setMessage({ tone: "error", text: t("businessNotFound") });
      } else if (step === "invalid") {
        setReasonError(t("reasonInvalid"));
        textarea.current?.focus();
      } else {
        setMessage({ tone: outcome.status === 503 ? "note" : "error", text: form(problem(outcome)) });
      }
    },
  });
  const { phase, busy } = recheck;

  useEffect(() => {
    if (open && phase === "reason") textarea.current?.focus();
  }, [open, phase]);

  function openPanel() {
    onOpen();
    setOpen(true);
  }

  function cancel() {
    setOpen(false);
    recheck.reset();
    setReason("");
    setReasonError(undefined);
    setMessage(null);
    requestAnimationFrame(() => opener.current?.focus());
  }

  function onSubmitReason(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    if (!reason.trim()) {
      setReasonError(t("reasonRequired"));
      textarea.current?.focus();
      return;
    }
    recheck.start();
  }

  if (!open) {
    return (
      <button
        ref={opener}
        className={`${styles.button} ${suspended ? styles.secondary : styles.danger}`}
        type="button"
        onClick={openPanel}
      >
        {t(suspended ? "reactivateAction" : "suspendAction")}
      </button>
    );
  }

  const describedBy = `${reasonId}-hint${reasonError ? ` ${reasonId}-error` : ""}`; // i18n-allow: ARIA description element identifiers.
  return (
    <div
      className={suspended ? styles.confirm : styles.dangerZone}
      role="group"
      aria-labelledby={titleId}
      aria-describedby={phase === "code" ? recheck.ledeId : undefined}
    >
      {recheck.banners}
      {message && <Banner tone={message.tone}>{message.text}</Banner>}
      {phase === "reason" ? (
        <form className={styles.form} onSubmit={onSubmitReason} noValidate>
          <strong id={titleId}>{t(suspended ? "reactivateTitle" : "suspendTitle", { name: business.name })}</strong>
          {!suspended && <p>{t("suspendBody")}</p>}
          <div className={styles.field}>
            <label className={styles.label} htmlFor={reasonId}>
              {t("reasonLabel")}
            </label>
            <div className={styles.input}>
              <textarea
                ref={textarea}
                id={reasonId}
                maxLength={500}
                value={reason}
                aria-required="true"
                aria-invalid={reasonError ? true : undefined}
                aria-describedby={describedBy}
                onChange={(event) => {
                  setReason(event.target.value);
                  setReasonError(undefined);
                }}
              />
            </div>
            <p className={styles.hint} id={`${reasonId}-hint`}>
              {t(suspended ? "reactivateHint" : "suspendHint")}
            </p>
            {reasonError && <FieldError id={`${reasonId}-error`}>{reasonError}</FieldError>}
          </div>
          <div className={styles.actions}>
            <button
              className={`${styles.button} ${suspended ? styles.primary : styles.danger}`}
              type="submit"
              aria-disabled={busy || undefined}
            >
              {busy ? form("sending") : t(suspended ? "reactivateAction" : "suspendAction")}
            </button>
            <button className={`${styles.button} ${styles.secondary}`} type="button" aria-disabled={busy || undefined} onClick={() => !busy && cancel()}>
              {keep}
            </button>
          </div>
        </form>
      ) : (
        recheck.codeForm
      )}
    </div>
  );
}
