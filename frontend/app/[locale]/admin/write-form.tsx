"use client";

import { useTranslations } from "next-intl";
import { type FormEvent, type ReactNode, useEffect, useId, useRef, useState } from "react";

import { Banner, FieldError, type Outcome, problem } from "../_ui/parts";
import styles from "../_ui/ui.module.css";
import admin from "./admin.module.css";
import { useSecondFactorRecheck } from "./recheck";

type Message = { tone: "error" | "note"; text: string };

// The refusals the billing writes name (Admin.billing.errors.*); anything else gets the generic line for its status.
const CODES = ["plan_taken", "plan_in_use", "plan_is_default", "plan_unavailable", "invalid_values", "invalid_price", "invalid_coupon", "payments_unavailable"] as const;
const known = (code?: string) => CODES.find((c) => c === code);

/** ZIF-78: the shape of every billing write. Its fields (children, state kept by the caller) and a reason; on submit
 * `run(reason)` sends the write, with the second-factor re-check of useSecondFactorRecheck. */
export function WriteForm({
  title,
  submit,
  danger,
  hint,
  children,
  check,
  onInvalid,
  run,
  onDone,
  onCancel,
}: {
  title: string;
  submit: string;
  danger?: boolean;
  hint?: string;
  children?: ReactNode;
  /** A message when the fields can't be sent yet (the caller marks and focuses the field). */
  check?: () => string | null;
  /** The server refused a feature's value (invalid_values): the caller marks and focuses it. */
  onInvalid?(key: string): void;
  run(reason: string): Promise<Outcome<unknown>>;
  /** The write succeeded; data is its answer. */
  onDone(data: unknown): void;
  onCancel(): void;
}) {
  const t = useTranslations("Admin");
  const form = useTranslations("Form");
  const titleId = useId();
  const reasonId = useId();
  const [reason, setReason] = useState("");
  const [reasonError, setReasonError] = useState<string>();
  const [message, setMessage] = useState<Message | null>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const heading = useRef<HTMLElement>(null);

  const recheck = useSecondFactorRecheck({
    titleId,
    cancelLabel: t("billing.cancel"),
    onCancel,
    attempt() {
      setMessage(null);
      return run(reason.trim());
    },
    onStep(step, outcome) {
      const code = known(outcome.code);
      if (outcome.status >= 200 && outcome.status < 300) onDone(outcome.data);
      else if (code) {
        setMessage({ tone: outcome.status === 503 ? "note" : "error", text: t(`billing.errors.${code}`, { key: outcome.key ?? "" }) });
        if (code === "invalid_values" && outcome.key) onInvalid?.(outcome.key);
      } else if (step === "notFound") setMessage({ tone: "error", text: t("billing.errors.gone") });
      else setMessage({ tone: outcome.status === 503 ? "note" : "error", text: form(problem(outcome)) });
    },
  });
  const { phase, busy } = recheck;

  // The form opens, and returns from the code form, at its title: the fields are above the reason.
  useEffect(() => {
    if (phase === "reason") heading.current?.focus();
  }, [phase]);

  function onSubmitReason(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    const refused = check?.();
    if (refused) {
      setMessage({ tone: "error", text: refused });
      return;
    }
    if (!reason.trim()) {
      setReasonError(t("reasonRequired"));
      textarea.current?.focus();
      return;
    }
    recheck.start();
  }

  const describedBy = `${reasonId}-hint${reasonError ? ` ${reasonId}-error` : ""}`; // i18n-allow: ARIA description element identifiers.
  return (
    <div className={danger ? styles.dangerZone : styles.confirm} role="group" aria-labelledby={titleId} aria-describedby={phase === "code" ? recheck.ledeId : undefined}>
      {recheck.banners}
      {message && <Banner tone={message.tone}>{message.text}</Banner>}
      {phase === "reason" ? (
        <form className={styles.form} onSubmit={onSubmitReason} noValidate>
          <strong ref={heading} id={titleId} tabIndex={-1} className={admin.quietFocus}>
            {title}
          </strong>
          {children}
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
              {hint ?? t("billing.reasonHint")}
            </p>
            {reasonError && <FieldError id={`${reasonId}-error`}>{reasonError}</FieldError>}
          </div>
          <div className={styles.actions}>
            <button className={`${styles.button} ${danger ? styles.danger : styles.primary}`} type="submit" aria-disabled={busy || undefined}>
              {busy ? form("sending") : submit}
            </button>
            <button className={`${styles.button} ${styles.secondary}`} type="button" aria-disabled={busy || undefined} onClick={() => !busy && onCancel()}>
              {t("billing.cancel")}
            </button>
          </div>
        </form>
      ) : (
        recheck.codeForm
      )}
    </div>
  );
}
