"use client";

import { useLocale, useTranslations } from "next-intl";
import { type FormEvent, type ReactNode, useId, useRef, useState } from "react";

import { setupComplete } from "@/api-client";
import { Link } from "@/i18n/navigation";

import { Banner, EmailField, FieldError, NoScript, Outcome, PasswordField, problem, send, Submit } from "../_ui/parts";
import styles from "../_ui/ui.module.css";

// The variable name stands out, so the operator can find it in their secrets.
const env = (chunks: ReactNode) => <span className={styles.chip}>{chunks}</span>;

const PASSWORD_CODES = ["password_too_short", "password_too_long", "password_too_common"] as const;

export function SetupForm() {
  const t = useTranslations("Setup");
  const form = useTranslations("Form");
  const locale = useLocale();
  const tokenId = useId();
  const nameId = useId();
  const tokenInput = useRef<HTMLInputElement>(null);
  const passwordInput = useRef<HTMLInputElement>(null);
  const [token, setToken] = useState("");
  const [tokenShown, setTokenShown] = useState(false);
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [ended, setEnded] = useState<"done" | "exists" | "notConfigured" | null>(null);
  const [tokenWrong, setTokenWrong] = useState(false);
  const [passwordError, setPasswordError] = useState<string>();
  const [message, setMessage] = useState<{ tone: "error" | "note"; text: string } | null>(null);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    const outcome = await send(setupComplete({ body: { token: token.trim(), email, password, name, locale: locale as "en" | "nl" | "pt" } }));
    setBusy(false);
    setTokenWrong(false);
    setPasswordError(undefined);
    setMessage(null);

    const code = PASSWORD_CODES.find((c) => c === outcome.code);
    if (outcome.status === 201) {
      setToken("");
      setPassword("");
      setEnded("done");
    } else if (outcome.status === 409) {
      setEnded("exists");
    } else if (outcome.code === "setup_not_configured") {
      setEnded("notConfigured");
    } else if (outcome.status === 403) {
      setTokenWrong(true);
      tokenInput.current?.focus();
    } else if (outcome.status === 429) {
      setMessage({ tone: "note", text: t("tooMany") });
    } else if (code) {
      setPasswordError(form(code));
      passwordInput.current?.focus();
    } else {
      setMessage({ tone: outcome.status === 503 ? "note" : "error", text: form(problem(outcome)) });
    }
  }

  if (ended === "done") {
    return (
      <Outcome icon="done" title={t("doneTitle")} lede={t("doneLede")}>
        <p className={styles.hint}>{t.rich("doneToken", { env })}</p>
        <p className={styles.hint}>{t("doneBusiness")}</p>
        <Link className={`${styles.button} ${styles.primary}`} href="/admin/sign-in">
          {t("doneContinue")}
        </Link>
      </Outcome>
    );
  }

  if (ended === "exists") {
    return (
      <Outcome icon="person" title={t("existsTitle")} lede={t("existsLede")}>
        <Link className={`${styles.button} ${styles.primary}`} href="/admin/sign-in">
          {t("signIn")}
        </Link>
      </Outcome>
    );
  }

  if (ended === "notConfigured") {
    return (
      <Outcome icon="key" title={t("notConfiguredTitle")} lede={t.rich("notConfiguredLede", { env })}>
        <button className={`${styles.button} ${styles.secondary}`} type="button" onClick={() => setEnded(null)}>
          {t("backToForm")}
        </button>
      </Outcome>
    );
  }

  return (
    <>
      <h1 className={styles.heading}>{t("title")}</h1>
      <p className={styles.lede}>{t("lede")}</p>
      <NoScript>{form("needsJavaScript")}</NoScript>
      {message && <Banner tone={message.tone}>{message.text}</Banner>}
      <form className={styles.form} method="post" onSubmit={onSubmit}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={tokenId}>
            {t("token")}
          </label>
          <div className={styles.input}>
            <input
              ref={tokenInput}
              id={tokenId}
              name="setup-token"
              className={styles.withToggle}
              type={tokenShown ? "text" : "password"}
              autoComplete="one-time-code"
              autoCapitalize="off"
              spellCheck={false}
              required
              maxLength={200}
              value={token}
              onChange={(event) => setToken(event.target.value)}
              aria-invalid={tokenWrong || undefined}
              aria-describedby={`${tokenId}-hint`}
            />
            <button
              className={styles.toggle}
              type="button"
              aria-controls={tokenId}
              aria-pressed={tokenShown}
              onClick={() => setTokenShown(!tokenShown)}
            >
              {tokenShown ? form("hide") : form("show")}
            </button>
          </div>
          {tokenWrong ? (
            <FieldError id={`${tokenId}-hint`}><span>{t.rich("wrongToken", { env })}</span></FieldError>
          ) : (
            <p className={styles.hint} id={`${tokenId}-hint`}>
              {t.rich("tokenHint", { env })}
            </p>
          )}
        </div>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={nameId}>
            {t("name")}
          </label>
          <div className={styles.input}>
            <input
              id={nameId}
              name="name"
              autoComplete="name"
              required
              maxLength={60}
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
          </div>
        </div>
        <EmailField value={email} onChange={setEmail} placeholder={t("emailPlaceholder")} />
        <PasswordField
          label={form("password")}
          value={password}
          onChange={setPassword}
          autoComplete="new-password"
          error={passwordError}
          inputRef={passwordInput}
        />
        <Submit busy={busy} busyLabel={t("creating")}>
          {t("submit")}
        </Submit>
      </form>
    </>
  );
}
