"use client";

import { useTranslations } from "next-intl";
import { type FormEvent, useEffect, useRef, useState } from "react";

import { adminPassSecondFactor, type EnrolmentOut, adminRead, adminSignIn, adminSignOut, type AdminSession } from "@/api-client";
import { Link, useRouter } from "@/i18n/navigation";
import { appCode, factorState, stepFor } from "@/lib/admin";

import { JSON_WRITE } from "../../_ui/console";
import { Banner, EmailField, Heading, Mark, NoScript, PasswordField, problem, Screen, send, Submit } from "../../_ui/parts";
import styles from "../../_ui/ui.module.css";
import admin from "../admin.module.css";
import { CodeField, EnrolCommand, SignedInAs } from "../parts";
import { RecoveryCodes, ScanStep, TicketStep } from "./enrol";

type Message = { tone: "error" | "note"; text: string };
type Step =
  | { kind: "loading" }
  | { kind: "password"; message?: Message }
  | { kind: "ticket"; who: AdminSession; message?: Message }
  | { kind: "scan"; who: AdminSession; enrolment: EnrolmentOut }
  | { kind: "codes"; who: AdminSession; codes: string[] }
  | { kind: "code"; who: AdminSession };

/**
 * The whole admin sign-in: password, then the second factor the session still needs. One route, because
 * the server decides the step (`second_factor`) and recovery codes must never survive a reload.
 */
export function AdminSignIn() {
  const t = useTranslations("Admin");
  const form = useTranslations("Form");
  const router = useRouter();
  const [step, setStep] = useState<Step>({ kind: "loading" });
  // After the first screen, each step change moves focus to its heading.
  const [moved, setMoved] = useState(false);
  // A ticket typed before the enrol command signed the admin out, kept (in memory only) for the next sign-in.
  const [kept, setKept] = useState({ ticket: "", email: "" });

  function route(who: AdminSession, focus = true) {
    if (focus) setMoved(true);
    const next = stepFor(who.second_factor);
    if (next !== "ticket") setKept({ ticket: "", email: "" });
    if (next === "done") router.replace("/admin");
    else setStep({ kind: next === "ticket" ? "ticket" : "code", who });
  }

  useEffect(() => {
    send(adminRead()).then((outcome) => {
      if (outcome.status === 200 && outcome.data) {
        route(outcome.data, false);
      } else {
        setStep({ kind: "password" });
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once, on load; route only closes over setters and router
  }, []);

  /** The session ended under a step: idle 30 minutes, or the enrol command, which signs the admin out. */
  function expired(ticket = "", email = "") {
    setMoved(true);
    setKept({ ticket, email });
    setStep({ kind: "password", message: { tone: "note", text: ticket ? t("ticketSignedOut") : t("expired") } });
  }

  async function leave() {
    await send(adminSignOut(JSON_WRITE));
    setKept({ ticket: "", email: "" });
    setMoved(true);
    setStep({ kind: "password" });
  }

  return (
    <Screen>
      {step.kind === "password" && <PasswordStep message={step.message} focus={moved} onSignedIn={route} />}
      {step.kind === "ticket" && (
        <TicketStep
          who={step.who}
          message={step.message}
          initialTicket={kept.email === step.who.email ? kept.ticket : ""}
          focus={moved}
          onBegun={(enrolment) => {
            setKept({ ticket: "", email: "" });
            setStep({ kind: "scan", who: step.who, enrolment });
          }}
          onEnrolled={() =>
            send(adminRead()).then((o) => {
              if (o.data) route(o.data);
              else if (o.status === 401) expired();
              else setStep({ kind: "ticket", who: step.who, message: { tone: o.status === 503 ? "note" : "error", text: form(problem(o)) } });
            })
          }
          onExpired={(ticket) => expired(ticket, step.who.email)}
        />
      )}
      {step.kind === "scan" && (
        <ScanStep
          who={step.who}
          enrolment={step.enrolment}
          onEnrolled={(codes) => setStep({ kind: "codes", who: step.who, codes })}
          onRestart={(message) => {
            setMoved(true);
            setStep({ kind: "ticket", who: step.who, message });
          }}
          onExpired={expired}
        />
      )}
      {step.kind === "codes" && <RecoveryCodes who={step.who} codes={step.codes} onDone={() => router.replace("/admin")} />}
      {step.kind === "code" && (
        <CodeStep who={step.who} focus={moved} onPassed={() => router.replace("/admin")} onEnrol={() => {
            setMoved(true);
            setStep({ kind: "ticket", who: step.who });
          }} onExpired={expired} />
      )}
      {(step.kind === "ticket" || step.kind === "scan" || step.kind === "code") && <SignedInAs email={step.who.email} onSignOut={leave} />}
    </Screen>
  );
}

function PasswordStep({ message: initial, focus, onSignedIn }: { message?: Message; focus: boolean; onSignedIn(who: AdminSession): void }) {
  const t = useTranslations("Admin");
  const form = useTranslations("Form");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<Message | null>(initial ?? null);
  const passwordInput = useRef<HTMLInputElement>(null);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    const outcome = await send(adminSignIn({ body: { email, password } }));
    setMessage(null);

    if (outcome.status === 200 && outcome.data) {
      onSignedIn(outcome.data); // busy stays on until the next screen replaces this one: a second Enter must not resubmit
      return;
    }
    setBusy(false);
    // 422 (say, a password over the length limit) gets the same single message: no hint about which part was wrong.
    if (outcome.status === 401 || outcome.status === 422) {
      setMessage({ tone: "error", text: t("invalid") });
      setPassword("");
      passwordInput.current?.focus();
    } else if (outcome.status === 429) {
      setMessage({ tone: "note", text: t("tooMany") });
    } else {
      setMessage({ tone: outcome.status === 503 ? "note" : "error", text: form(problem(outcome)) });
    }
  }

  return (
    <>
      <Mark icon="shield" />
      <Heading focus={focus}>{t("title")}</Heading>
      <p className={styles.lede}>{t("lede")}</p>
      <NoScript>{form("needsJavaScript")}</NoScript>
      {message && <Banner tone={message.tone}>{message.text}</Banner>}
      <form className={styles.form} method="post" onSubmit={onSubmit}>
        <EmailField value={email} onChange={setEmail} />
        <PasswordField
          label={form("password")}
          value={password}
          onChange={setPassword}
          autoComplete="current-password"
          inputRef={passwordInput}
          labelAside={<Link href="/forgot-password">{t("forgot")}</Link>}
        />
        <Submit busy={busy} busyLabel={t("signingIn")}>
          {t("submit")}
        </Submit>
      </form>
      <p className={styles.aside}>
        {t("business")} <Link href="/sign-in">{t("businessLink")}</Link>
      </p>
    </>
  );
}

function CodeStep({ who, focus, onPassed, onEnrol, onExpired }: { who: AdminSession; focus: boolean; onPassed(): void; onEnrol(): void; onExpired(): void }) {
  const t = useTranslations("Admin");
  const form = useTranslations("Form");
  const [kind, setKind] = useState<"app" | "recovery">("app");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [message, setMessage] = useState<Message | null>(null);
  // Locked or unavailable: app codes can't work now, so the recovery field is the only way in.
  const [blocked, setBlocked] = useState<"locked" | "unavailable" | null>(null);
  const input = useRef<HTMLInputElement>(null);

  function switchTo(next: "app" | "recovery") {
    setKind(next);
    setCode("");
    setError(undefined);
    requestAnimationFrame(() => input.current?.focus());
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    const value = kind === "app" ? appCode(code) : code.trim();
    if (!value) {
      setError(t("codeFormat"));
      input.current?.focus();
      return;
    }
    setBusy(true);
    const outcome = await send(adminPassSecondFactor({ body: { code: value } }));
    const state = factorState(outcome.status, outcome.code);
    setError(undefined);
    setMessage(null);

    if (outcome.status === 200) {
      onPassed(); // busy stays on until navigation: a second Enter would replay the code and count as a failure
      return;
    }
    setBusy(false);
    if (state === "wrong") {
      setError(kind === "app" ? t("wrongAppCode") : t("wrongRecovery"));
      setCode("");
      input.current?.focus();
    } else if (state === "expired") {
      onExpired();
    } else if (state === "locked" || state === "unavailable") {
      setBlocked(state);
      switchTo("recovery");
    } else if (state === "conflict") {
      onEnrol();
    } else if (state === "tooMany") {
      setMessage({ tone: "note", text: t("tooMany") });
    } else {
      setMessage({ tone: outcome.status === 503 ? "note" : "error", text: form(problem(outcome)) });
    }
  }

  return (
    <>
      <Mark icon="shield" />
      <Heading focus={focus}>{kind === "app" ? t("codeTitle") : t("recoveryTitle")}</Heading>
      <p className={styles.lede}>{kind === "app" ? t("codeLede") : t("recoveryLede")}</p>
      {blocked === "locked" && <Banner tone="error">{t("locked")}</Banner>}
      {blocked === "unavailable" && <Banner tone="note">{t("appUnavailable")}</Banner>}
      {message && <Banner tone={message.tone}>{message.text}</Banner>}
      <form className={styles.form} method="post" onSubmit={onSubmit} noValidate>
        <CodeField
          kind={kind}
          label={kind === "app" ? t("appCode") : t("recoveryCode")}
          value={code}
          onChange={setCode}
          error={error}
          hint={kind === "recovery" ? t("recoveryHint") : undefined}
          inputRef={input}
        />
        <Submit busy={busy} busyLabel={t("verifying")}>
          {t("verify")}
        </Submit>
      </form>
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
      {kind === "recovery" && (
        <div className={admin.section}>
          <div className={styles.divider} />
          <h2 className={admin.sectionTitle}>{t("noCodesLeft")}</h2>
          <p className={styles.hint}>{t("resetIntro")}</p>
          <EnrolCommand email={who.email} />
        </div>
      )}
    </>
  );
}
