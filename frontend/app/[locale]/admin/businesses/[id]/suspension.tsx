"use client";

import { useTranslations } from "next-intl";
import { type FormEvent, useEffect, useId, useRef, useState } from "react";

import { useRouter } from "@/i18n/navigation";

import { adminPassSecondFactor, adminReactivate, adminSuspend, type BusinessDetail } from "@/api-client";
import { appCode, factorState, writeStep } from "@/lib/admin";

import { Banner, FieldError, problem, send } from "../../../_ui/parts";
import styles from "../../../_ui/ui.module.css";
import admin from "../../admin.module.css";
import { CodeField } from "../../parts";

export type Done = { tone: "info" | "note"; text: string };
type Message = { tone: "error" | "note"; text: string };

/** ZIF-73: suspend or reactivate this business, with a reason, in place at the foot of the detail. A stale
 * second factor (writes need one from the last 15 minutes) swaps the reason form for a code form, keeps the
 * reason and sends the write again once the code passes. */
export function Suspension({ business, onOpen, onChanged }: { business: BusinessDetail; onOpen(): void; onChanged(done: Done): void }) {
  const t = useTranslations("Admin");
  const form = useTranslations("Form");
  const router = useRouter();
  const suspended = business.suspended_at !== null;
  const titleId = useId();
  const ledeId = useId();
  const reasonId = useId();
  const [open, setOpen] = useState(false);
  const [phase, setPhase] = useState<"reason" | "code">("reason");
  const [reason, setReason] = useState("");
  const [reasonError, setReasonError] = useState<string>();
  const [kind, setKind] = useState<"app" | "recovery">("app");
  const [code, setCode] = useState("");
  const [codeError, setCodeError] = useState<string>();
  const [blocked, setBlocked] = useState<false | "locked" | "unavailable">(false); // only a recovery code can work now
  const [message, setMessage] = useState<Message | null>(null);
  const [busy, setBusy] = useState(false);
  const submitting = useRef(false);
  const opener = useRef<HTMLButtonElement>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const title = useRef<HTMLElement>(null);
  const codeInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    (phase === "code" ? title : textarea).current?.focus();
  }, [open, phase]);

  function openPanel() {
    onOpen();
    setOpen(true);
  }

  function cancel() {
    if (busy) return;
    setOpen(false);
    setPhase("reason");
    setReason("");
    setReasonError(undefined);
    setCode("");
    setCodeError(undefined);
    setMessage(null);
    requestAnimationFrame(() => opener.current?.focus());
  }

  // The admin's own switch: a no-op while a check is in flight. The lock path below sets the kind itself.
  function switchTo(next: "app" | "recovery") {
    if (busy) return;
    setKind(next);
    setCode("");
    setCodeError(undefined);
    requestAnimationFrame(() => codeInput.current?.focus());
  }

  // Sends the write with the kept reason and acts on writeStep: the same for a first submit and the resend.
  async function attempt() {
    setMessage(null);
    setReasonError(undefined);
    const body = { reason: reason.trim() };
    const outcome = await send((suspended ? adminReactivate : adminSuspend)({ path: { tenant_id: business.id }, body }));
    const step = writeStep(outcome.status, outcome.code);
    if (step === "done") {
      setOpen(false);
      onChanged({ tone: "info", text: t(suspended ? "reactivatedDone" : "suspendedDone") });
    } else if (step === "recheck") {
      setPhase("code");
    } else if (step === "signIn") {
      router.replace("/admin/sign-in");
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
  }

  async function guarded(work: () => Promise<void>) {
    if (submitting.current) return;
    submitting.current = true;
    setBusy(true);
    try {
      await work();
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }

  function onSubmitReason(event: FormEvent) {
    event.preventDefault();
    if (submitting.current) return;
    if (!reason.trim()) {
      setReasonError(t("reasonRequired"));
      textarea.current?.focus();
      return;
    }
    void guarded(attempt);
  }

  // The code form's answers follow CodeStep (admin-sign-in.tsx): same states, same keys. It is not exported, as its
  // heading and mark belong to the sign-in screen.
  function onSubmitCode(event: FormEvent) {
    event.preventDefault();
    if (submitting.current) return;
    const value = kind === "app" ? appCode(code) : code.trim();
    if (!value) {
      setCodeError(t("codeFormat"));
      codeInput.current?.focus();
      return;
    }
    void guarded(async () => {
      const outcome = await send(adminPassSecondFactor({ body: { code: value } }));
      const state = factorState(outcome.status, outcome.code);
      setCodeError(undefined);
      setMessage(null);
      if (outcome.status === 200) {
        setCode(""); // a used code is never left in the field
        setPhase("reason");
        await attempt();
      } else if (state === "wrong") {
        setCodeError(kind === "app" ? t("wrongAppCode") : t("wrongRecovery"));
        setCode("");
        codeInput.current?.focus();
      } else if (state === "expired" || state === "conflict") {
        router.replace("/admin/sign-in");
      } else if (state === "locked" || state === "unavailable") {
        setBlocked(state);
        setKind("recovery");
        setCodeError(undefined);
        setCode("");
        requestAnimationFrame(() => codeInput.current?.focus());
      } else if (state === "tooMany") {
        setMessage({ tone: "note", text: t("tooMany") });
      } else {
        setMessage({ tone: outcome.status === 503 ? "note" : "error", text: form(problem(outcome)) });
      }
    });
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

  const describedBy = `${reasonId}-hint${reasonError ? ` ${reasonId}-error` : ""}`;
  return (
    <div
      className={suspended ? styles.confirm : styles.dangerZone}
      role="group"
      aria-labelledby={titleId}
      aria-describedby={phase === "code" ? ledeId : undefined}
    >
      {phase === "code" && blocked === "locked" && <Banner tone="error">{t("locked")}</Banner>}
      {phase === "code" && blocked === "unavailable" && <Banner tone="note">{t("appUnavailable")}</Banner>}
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
            <button className={`${styles.button} ${styles.secondary}`} type="button" aria-disabled={busy || undefined} onClick={cancel}>
              {t(suspended ? "keepSuspended" : "keepRunning")}
            </button>
          </div>
        </form>
      ) : (
        <form className={styles.form} onSubmit={onSubmitCode} noValidate>
          <strong ref={title} id={titleId} tabIndex={-1} className={admin.quietFocus}>
            {t("recheckTitle")}
          </strong>
          <p id={ledeId}>{t("recheckLede")}</p>
          <CodeField
            kind={kind}
            label={kind === "app" ? t("appCode") : t("recoveryCode")}
            value={code}
            onChange={setCode}
            error={codeError}
            hint={kind === "recovery" ? t("recoveryHint") : undefined}
            inputRef={codeInput}
          />
          <div className={styles.actions}>
            <button className={`${styles.button} ${styles.primary}`} type="submit" aria-disabled={busy || undefined}>
              {busy ? t("checking") : t("continue")}
            </button>
            <button className={`${styles.button} ${styles.secondary}`} type="button" aria-disabled={busy || undefined} onClick={cancel}>
              {t(suspended ? "keepSuspended" : "keepRunning")}
            </button>
          </div>
          {kind === "app" ? (
            <button className={styles.textButton} type="button" onClick={() => switchTo("recovery")}>
              {t("useRecovery")}
            </button>
          ) : (
            !blocked && (
              <button className={styles.textButton} type="button" onClick={() => switchTo("app")}>
                {t("useApp")}
              </button>
            )
          )}
        </form>
      )}
    </div>
  );
}
