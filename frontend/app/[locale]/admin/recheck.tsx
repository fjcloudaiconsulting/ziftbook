"use client";

import { useTranslations } from "next-intl";
import { type FormEvent, type ReactNode, useEffect, useId, useRef, useState } from "react";

import { useRouter } from "@/i18n/navigation";

import { adminPassSecondFactor } from "@/api-client";
import { appCode } from "@/lib/admin";
import { type Event, initialRecheck, type Recheck, recheck, type Then } from "@/lib/admin-write";

import { Banner, type Outcome, problem, send } from "../_ui/parts";
import styles from "../_ui/ui.module.css";
import admin from "./admin.module.css";
import { CodeField } from "./parts";

type Notice = { tone: "error" | "note"; text: string };
type Step = Exclude<Then, "recheck" | "signIn" | "resend" | "wrong" | "blocked" | "tooMany">;

/** The second-factor re-check shared by the admin writes (ZIF-73 suspension, ZIF-78 billing): writes need a code from
 * the last 15 minutes. `start()` sends the write (`attempt`); a stale factor (403) swaps the reason form for the
 * code form, and a passed code sends the write again. `onStep` gets every other answer (done, changed, notFound,
 * invalid, other); sign-in is handled here. The phases live in lib/admin-write.ts. */
export function useSecondFactorRecheck({
  attempt,
  onStep,
  onCancel,
  cancelLabel,
  titleId,
}: {
  attempt(): Promise<Outcome<unknown>>;
  onStep(step: Step, outcome: Outcome<unknown>): void;
  onCancel(): void;
  cancelLabel: string;
  /** The group's label: the code form's heading carries it. */
  titleId: string;
}) {
  const t = useTranslations("Admin");
  const form = useTranslations("Form");
  const router = useRouter();
  const ledeId = useId();
  const [state, setState] = useState<Recheck>(initialRecheck);
  const [code, setCode] = useState("");
  const [codeError, setCodeError] = useState<string>();
  const [notice, setNotice] = useState<Notice | null>(null);
  const [busy, setBusy] = useState(false);
  const current = useRef(state);
  const submitting = useRef(false);
  const latest = useRef({ attempt, onStep });
  const heading = useRef<HTMLElement>(null);
  const codeInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    latest.current = { attempt, onStep }; // the resend after a code runs the newest closures
  });

  useEffect(() => {
    if (state.phase === "code") heading.current?.focus();
  }, [state.phase]);

  function apply(event: Event): Then | undefined {
    const next = recheck(current.current, event);
    current.current = next.state;
    setState(next.state);
    return next.then;
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

  async function sendWrite() {
    setNotice(null);
    const outcome = await latest.current.attempt();
    const then = apply({ type: "write", status: outcome.status, code: outcome.code });
    if (then === "signIn") router.replace("/admin/sign-in");
    else if (then && then !== "recheck") latest.current.onStep(then as Step, outcome);
  }

  function onSubmitCode(event: FormEvent) {
    event.preventDefault();
    if (submitting.current) return;
    const value = state.kind === "app" ? appCode(code) : code.trim();
    if (!value) {
      setCodeError(t("codeFormat"));
      codeInput.current?.focus();
      return;
    }
    void guarded(async () => {
      const outcome = await send(adminPassSecondFactor({ body: { code: value } }));
      const then = apply({ type: "code", status: outcome.status, code: outcome.code });
      setCodeError(undefined);
      setNotice(null);
      if (then === "resend") {
        setCode(""); // a used code is never left in the field
        await sendWrite();
      } else if (then === "wrong") {
        setCodeError(state.kind === "app" ? t("wrongAppCode") : t("wrongRecovery"));
        setCode("");
        codeInput.current?.focus();
      } else if (then === "signIn") {
        router.replace("/admin/sign-in");
      } else if (then === "blocked") {
        setCode("");
        requestAnimationFrame(() => codeInput.current?.focus());
      } else if (then === "tooMany") {
        setNotice({ tone: "note", text: t("tooMany") });
      } else {
        setNotice({ tone: outcome.status === 503 ? "note" : "error", text: form(problem(outcome)) });
      }
    });
  }

  function switchTo(kind: "app" | "recovery") {
    if (busy) return;
    apply({ type: "switch", kind });
    setCode("");
    setCodeError(undefined);
    requestAnimationFrame(() => codeInput.current?.focus());
  }

  const banners: ReactNode = (
    <>
      {state.phase === "code" && state.blocked === "locked" && <Banner tone="error">{t("locked")}</Banner>}
      {state.phase === "code" && state.blocked === "unavailable" && <Banner tone="note">{t("appUnavailable")}</Banner>}
      {notice && <Banner tone={notice.tone}>{notice.text}</Banner>}
    </>
  );

  const codeForm: ReactNode = (
    <form className={styles.form} onSubmit={onSubmitCode} noValidate>
      <strong ref={heading} id={titleId} tabIndex={-1} className={admin.quietFocus}>
        {t("recheckTitle")}
      </strong>
      <p id={ledeId}>{t("recheckLede")}</p>
      <CodeField
        kind={state.kind}
        label={state.kind === "app" ? t("appCode") : t("recoveryCode")}
        value={code}
        onChange={setCode}
        error={codeError}
        hint={state.kind === "recovery" ? t("recoveryHint") : undefined}
        inputRef={codeInput}
      />
      <div className={styles.actions}>
        <button className={`${styles.button} ${styles.primary}`} type="submit" aria-disabled={busy || undefined}>
          {busy ? t("checking") : t("continue")}
        </button>
        <button className={`${styles.button} ${styles.secondary}`} type="button" aria-disabled={busy || undefined} onClick={() => !busy && onCancel()}>
          {cancelLabel}
        </button>
      </div>
      {state.kind === "app" ? (
        <button className={styles.textButton} type="button" onClick={() => switchTo("recovery")}>
          {t("useRecovery")}
        </button>
      ) : (
        !state.blocked && (
          <button className={styles.textButton} type="button" onClick={() => switchTo("app")}>
            {t("useApp")}
          </button>
        )
      )}
    </form>
  );

  return {
    phase: state.phase,
    busy,
    ledeId,
    banners,
    codeForm,
    /** Sends the write; a no-op while one is in flight. */
    start: () => void guarded(sendWrite),
    /** Back to the reason phase with nothing typed, as when a panel closes. */
    reset() {
      apply({ type: "reset" });
      setCode("");
      setCodeError(undefined);
      setNotice(null);
    },
  };
}
