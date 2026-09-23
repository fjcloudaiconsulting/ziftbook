"use client";

import { useTranslations } from "next-intl";
import { type ReactNode, type Ref, useEffect, useId, useRef, useState, useSyncExternalStore } from "react";

import { clock, RESEND_AFTER_MS, secondsLeft, takeToken } from "@/lib/account";

import { Header } from "./header";
import styles from "./ui.module.css";

/** What an API call came back with: status 0 means the request never got an answer.
 * `weekday`: only `outside_opening_hours` carries it (`errors.py:8-13`), so the working-hours
 * screen can name the day the server refused. */
export type Outcome<T> = { status: number; code?: string; weekday?: number; data?: T };

export async function send<T>(request: Promise<{ data?: T; error?: unknown; response?: Response }>): Promise<Outcome<T>> {
  try {
    const { data, error, response } = await request;
    const body = error as { code?: string; weekday?: number | null } | undefined;
    return { status: response?.status ?? 0, code: body?.code, weekday: body?.weekday ?? undefined, data };
  } catch {
    return { status: 0 };
  }
}

/** The message for an answer no screen handles itself. */
export function problem(outcome: Outcome<unknown>): "busy" | "unreachable" | "invalidRequest" | "unexpected" {
  if (outcome.status === 0) return "unreachable";
  if (outcome.status === 503) return "busy";
  if (outcome.status === 422) return "invalidRequest";
  return "unexpected";
}

export function Screen({ children }: { children: ReactNode }) {
  return (
    <>
      <Header />
      <main className={styles.screen}>
        <div className={styles.col}>{children}</div>
      </main>
    </>
  );
}

const ICONS = {
  alert: <path d="M8 4.5v4M8 11h.01" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />,
  clock: <path d="M8 4.5V8l2.2 1.4" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />,
  info: <path d="M8 7.5V11M8 5h.01" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />,
};

/** An error (role alert) or a neutral note (role status): colour, icon and words change together. */
export function Banner({ tone, children }: { tone: "error" | "note" | "info"; children: ReactNode }) {
  const icon = tone === "error" ? "alert" : tone === "note" ? "clock" : "info";
  return (
    <div className={tone === "error" ? styles.banner : `${styles.banner} ${styles.note}`} role={tone === "error" ? "alert" : "status"}>
      <svg aria-hidden="true" viewBox="0 0 16 16" width="16" height="16">
        <circle cx="8" cy="8" r="6.5" fill="none" stroke="currentColor" strokeWidth="1.6" />
        {ICONS[icon]}
      </svg>
      <span>{children}</span>
    </div>
  );
}

const MARKS = {
  calendar: (
    <>
      <rect x="3.5" y="5" width="17" height="15" rx="2.5" fill="none" stroke="currentColor" strokeWidth="1.8" />
      <path d="M3.5 9.5h17M8 3.5v3M16 3.5v3" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </>
  ),
  mail: (
    <>
      <rect x="3.5" y="5.5" width="17" height="13" rx="2" fill="none" stroke="currentColor" strokeWidth="1.8" />
      <path d="M4 7l8 6 8-6" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" />
    </>
  ),
  expired: (
    <>
      <circle cx="12" cy="12" r="8.25" fill="none" stroke="currentColor" strokeWidth="1.8" />
      <path d="M12 7.5v5l3 2" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </>
  ),
  person: (
    <>
      <circle cx="12" cy="9" r="3.5" fill="none" stroke="currentColor" strokeWidth="1.8" />
      <path d="M5 19.5c1.2-3.3 3.8-5 7-5s5.8 1.7 7 5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </>
  ),
  done: <path d="M5.5 12.5l4 4 9-9" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />,
};

export function Mark({ icon }: { icon: keyof typeof MARKS }) {
  return (
    <span className={icon === "done" ? `${styles.mark} ${styles.markOk}` : styles.mark} aria-hidden="true">
      <svg viewBox="0 0 24 24" width="24" height="24">
        {MARKS[icon]}
      </svg>
    </span>
  );
}

/**
 * A screen's heading. focus: the screen replaced another one without a page load, so move focus here and
 * screen readers announce where the person is now.
 */
export function Heading({ focus, children }: { focus?: boolean; children: ReactNode }) {
  const ref = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (focus) ref.current?.focus();
  }, [focus]);
  return (
    <h1 ref={ref} className={styles.heading} tabIndex={focus ? -1 : undefined}>
      {children}
    </h1>
  );
}

/** Where a flow ends: a mark, what happened, and the one way forward. */
export function Outcome({ icon, title, lede, children }: { icon: keyof typeof MARKS; title: string; lede: string; children: ReactNode }) {
  return (
    <>
      <Mark icon={icon} />
      <Heading focus>{title}</Heading>
      <p className={styles.lede}>{lede}</p>
      <div className={styles.stack}>{children}</div>
    </>
  );
}

/** Pages whose forms only work with JavaScript say so when it's off. */
export function NoScript({ children }: { children: ReactNode }) {
  return (
    <noscript>
      <Banner tone="info">{children}</Banner>
    </noscript>
  );
}

/** Stays focusable while a request runs, so focus isn't lost; a second press does nothing.
 * `form`: the id of the `<form>` to submit, for a submit button rendered outside it (a week
 * editor's savebar, portaled into the shell's own bottom bar on phone). */
export function Submit({ busy, busyLabel, form, children }: { busy: boolean; busyLabel: string; form?: string; children: ReactNode }) {
  return (
    <button className={`${styles.button} ${styles.primary}`} type="submit" form={form} aria-disabled={busy || undefined}>
      {busy && <span className={styles.spinner} aria-hidden="true" />}
      {busy ? busyLabel : children}
    </button>
  );
}

export function FieldError({ id, children }: { id: string; children: ReactNode }) {
  return (
    <p className={styles.fieldError} id={id}>
      <svg aria-hidden="true" viewBox="0 0 16 16" width="14" height="14">
        <circle cx="8" cy="8" r="6.5" fill="none" stroke="currentColor" strokeWidth="1.6" />
        {ICONS.alert}
      </svg>
      {children}
    </p>
  );
}

export function EmailField({ value, onChange, error }: { value: string; onChange(value: string): void; error?: string }) {
  const t = useTranslations("Form");
  const id = useId();
  return (
    <div className={styles.field}>
      <label className={styles.label} htmlFor={id}>
        {t("email")}
      </label>
      <div className={styles.input}>
        <input
          id={id}
          name="email"
          type="email"
          autoComplete="email"
          required
          maxLength={254}
          placeholder={t("emailPlaceholder")}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? `${id}-error` : undefined}
        />
      </div>
      {error && <FieldError id={`${id}-error`}>{error}</FieldError>}
    </div>
  );
}

type PasswordFieldProps = {
  label: string;
  value: string;
  onChange(value: string): void;
  /** "current-password" signs in; "new-password" shows the rules. */
  autoComplete: "current-password" | "new-password";
  error?: string;
  labelAside?: ReactNode;
  inputRef?: Ref<HTMLInputElement>;
};

export function PasswordField({ label, value, onChange, autoComplete, error, labelAside, inputRef }: PasswordFieldProps) {
  const t = useTranslations("Form");
  const id = useId();
  const [shown, setShown] = useState(false);
  const isNew = autoComplete === "new-password";
  const describedBy = [error && `${id}-error`, isNew && !error && `${id}-rules`].filter(Boolean).join(" ") || undefined;
  // Characters as the server counts them (code points), so the rule and the server agree.
  const long = [...value].length >= 12;

  return (
    <div className={styles.field}>
      <div className={styles.labelRow}>
        <label className={styles.label} htmlFor={id}>
          {label}
        </label>
        {labelAside}
      </div>
      <div className={styles.input}>
        <input
          ref={inputRef}
          id={id}
          name="password"
          className={styles.withToggle}
          type={shown ? "text" : "password"}
          autoComplete={autoComplete}
          required
          value={value}
          onChange={(event) => onChange(event.target.value)}
          aria-invalid={error ? true : undefined}
          aria-describedby={describedBy}
        />
        <button className={styles.toggle} type="button" aria-controls={id} aria-pressed={shown} onClick={() => setShown(!shown)}>
          {shown ? t("hide") : t("show")}
        </button>
      </div>
      {error && <FieldError id={`${id}-error`}>{error}</FieldError>}
      {isNew && !error && (
        <ul className={styles.rules} id={`${id}-rules`} aria-label={t("rules")}>
          <li className={long ? styles.met : undefined}>
            <svg aria-hidden="true" viewBox="0 0 16 16" width="14" height="14">
              {long ? (
                <path d="M3 8.5l3 3 7-7" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
              ) : (
                <circle cx="8" cy="8" r="2.5" fill="currentColor" />
              )}
            </svg>
            {t("ruleLength")}
          </li>
          <li>
            <svg aria-hidden="true" viewBox="0 0 16 16" width="14" height="14">
              <circle cx="8" cy="8" r="2.5" fill="currentColor" />
            </svg>
            {t("ruleUnique")}
          </li>
        </ul>
      )}
    </div>
  );
}

/** Seconds until another link may be sent, ticking while there are any. */
function useSecondsLeft(sentAt: number): number {
  const [now, setNow] = useState(sentAt);
  useEffect(() => {
    const timer = setInterval(() => {
      const current = Date.now();
      setNow(current);
      if (current >= sentAt + RESEND_AFTER_MS) clearInterval(timer);
    }, 250);
    return () => clearInterval(timer);
  }, [sentAt]);
  return secondsLeft(sentAt, Math.max(now, sentAt));
}

type CheckInboxProps = {
  email: string;
  lede: "signUpLede" | "resetLede";
  /** Sends the link again; resolves with when it was sent, or null if it wasn't. */
  resend(): Promise<number | null>;
  differentEmail(): void;
  sentAt: number;
  error?: ReactNode;
};

export function CheckInbox({ email, lede, resend, differentEmail, sentAt, error }: CheckInboxProps) {
  const t = useTranslations("Inbox");
  const left = useSecondsLeft(sentAt);
  const [sending, setSending] = useState(false);

  async function onResend() {
    if (left > 0 || sending) return;
    setSending(true);
    await resend();
    setSending(false);
  }

  return (
    <>
      <Mark icon="mail" />
      <Heading focus>{t("title")}</Heading>
      <p className={styles.lede}>{t.rich(lede, { email, chip: (chunks) => <span className={styles.chip}>{chunks}</span> })}</p>
      <p className={styles.hint}>{t("hint")}</p>
      {error}
      <div className={styles.stack}>
        <button
          className={`${styles.button} ${styles.secondary}`}
          type="button"
          aria-disabled={left > 0 || sending || undefined}
          onClick={onResend}
        >
          {left > 0 ? (
            <>
              <svg aria-hidden="true" viewBox="0 0 20 20" width="18" height="18">
                <circle cx="10" cy="10" r="8" fill="none" stroke="currentColor" strokeOpacity="0.25" strokeWidth="2.2" />
                <circle
                  cx="10"
                  cy="10"
                  r="8"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2.2"
                  strokeLinecap="round"
                  pathLength={60}
                  strokeDasharray={`${left} 60`}
                  transform="rotate(-90 10 10)"
                />
              </svg>
              <span className={styles.tabular}>{t("resendIn", { time: clock(left) })}</span>
            </>
          ) : (
            t("resend")
          )}
        </button>
        {/* Announced once per countdown: the text appears when the button unlocks. */}
        <p className={styles.srOnly} role="status">
          {left === 0 ? t("resendReady") : ""}
        </p>
        <p className={styles.aside}>
          <button className={styles.textButton} type="button" onClick={differentEmail}>
            {t("differentEmail")}
          </button>
        </p>
      </div>
    </>
  );
}

const taken = new Map<string, string>();

function onHashChange(changed: () => void) {
  window.addEventListener("hashchange", changed);
  return () => window.removeEventListener("hashchange", changed);
}

/**
 * The token from the link that opened this page: undefined on the server and while hydrating, so the
 * server-rendered page is the no-JavaScript one. A new link pasted into the same tab only changes the
 * fragment, so the page listens for that too.
 */
export function useLinkToken(key: string): string | null | undefined {
  return useSyncExternalStore(
    onHashChange,
    () => {
      const token = takeToken(window);
      if (token) taken.set(key, token);
      return taken.get(key) ?? null;
    },
    () => undefined,
  );
}

/** Once a link has been used, the page doesn't offer its form again. */
export function forgetToken(key: string) {
  taken.delete(key);
}
