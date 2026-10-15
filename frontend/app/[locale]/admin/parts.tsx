"use client";

import { useTranslations } from "next-intl";
import { type RefObject, type Ref, useEffect, useId, useRef, useState } from "react";

import { enrolCommand } from "@/lib/admin";

import { FieldError } from "../_ui/parts";
import styles from "../_ui/ui.module.css";
import admin from "./admin.module.css";

/**
 * Copies `text`, then says so for two seconds (to sighted and screen-reader users alike). When the clipboard
 * is refused, `target`'s text is selected so one more keystroke copies it, and the button says it failed.
 */
export function CopyButton({ text, label, target }: { text: string; label: string; target?: RefObject<HTMLElement | null> }) {
  const t = useTranslations("Admin");
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);

  async function onClick() {
    // Empty the live region first: a second "Copied" into an unchanged region isn't announced.
    setState("idle");
    clearTimeout(timer.current);
    let next: "copied" | "failed" = "copied";
    try {
      if (!navigator.clipboard) throw new Error("no clipboard");
      await navigator.clipboard.writeText(text);
    } catch {
      next = "failed";
      // A tick of "idle" first, or a second failure in a row batches into an unchanged live region.
      await new Promise((resolve) => setTimeout(resolve, 0));
      if (target?.current) {
        const range = document.createRange();
        range.selectNodeContents(target.current);
        const selection = window.getSelection();
        selection?.removeAllRanges();
        selection?.addRange(range);
      }
    }
    setState(next);
    timer.current = setTimeout(() => setState("idle"), 2000);
  }

  return (
    <button className={`${styles.button} ${styles.secondary} ${styles.small} ${admin.copy}`} type="button" onClick={onClick}>
      {state === "copied" && (
        <svg aria-hidden="true" viewBox="0 0 16 16" width="16" height="16">
          <path d="M3 8.5l3 3 7-7" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      )}
      <span aria-live="polite">{state === "copied" ? t("copied") : state === "failed" ? t("copyFailed") : label}</span>
    </button>
  );
}

/** The CLI command that issues a setup ticket, for whoever runs this ziftbook's server. */
export function EnrolCommand({ email }: { email: string }) {
  const t = useTranslations("Admin");
  // The command itself is never translated; only the placeholder for the reason is.
  const reason = `--reason "${t("reasonPlaceholder")}"`;
  const head = enrolCommand(email);
  const code = useRef<HTMLElement>(null);
  return (
    <div className={admin.command}>
      <code ref={code} className={admin.mono}>
        {`${head} `}
        <span className={admin.nowrap}>{reason}</span>
      </code>
      <CopyButton text={`${head} ${reason}`} label={t("copy")} target={code} />
    </div>
  );
}

/** A 6-digit app code, or a recovery code: one field, two shapes. */
export function CodeField({
  kind,
  label,
  value,
  onChange,
  error,
  hint,
  inputRef,
}: {
  kind: "app" | "recovery";
  label: string;
  value: string;
  onChange(value: string): void;
  error?: string;
  hint?: string;
  inputRef?: Ref<HTMLInputElement>;
}) {
  const id = useId();
  const described = error ? `${id}-error` : hint ? `${id}-hint` : undefined;
  return (
    <div className={styles.field}>
      <label className={styles.label} htmlFor={id}>
        {label}
      </label>
      <div className={`${styles.input} ${kind === "app" ? admin.otp : admin.recovery}`}>
        {kind === "app" ? (
          <input
            ref={inputRef}
            id={id}
            key="app"
            name="code"
            className={admin.mono}
            inputMode="numeric"
            autoComplete="one-time-code"
            placeholder="000000"
            maxLength={7}
            required
            value={value}
            onChange={(event) => onChange(event.target.value)}
            aria-invalid={error ? true : undefined}
            aria-describedby={described}
          />
        ) : (
          <input
            ref={inputRef}
            id={id}
            key="recovery"
            name="recovery-code"
            className={admin.mono}
            autoComplete="off"
            autoCapitalize="characters"
            spellCheck={false}
            placeholder="XXXX-XXXX-XXXX-XXXX"
            maxLength={40}
            required
            value={value}
            onChange={(event) => onChange(event.target.value)}
            aria-invalid={error ? true : undefined}
            aria-describedby={described}
          />
        )}
      </div>
      {error ? (
        <FieldError id={`${id}-error`}>{error}</FieldError>
      ) : (
        hint && (
          <p className={styles.hint} id={`${id}-hint`}>
            {hint}
          </p>
        )
      )}
    </div>
  );
}

export function SignedInAs({ email, onSignOut }: { email: string; onSignOut(): void }) {
  const t = useTranslations("Admin");
  return (
    <p className={admin.who}>
      <span>{t.rich("signedInAs", { email, chip: (chunks) => <span className={styles.chip}>{chunks}</span> })}</span>
      <button className={styles.textButton} type="button" onClick={onSignOut}>
        {t("signOut")}
      </button>
    </p>
  );
}
