"use client";

import { useTranslations } from "next-intl";
import { type FormEvent, useState } from "react";

import { accountCompletePasswordReset } from "@/api-client";
import { Link } from "@/i18n/navigation";

import { Banner, forgetToken, NoScript, Outcome, PasswordField, problem, send, Submit, useLinkToken } from "../_ui/parts";
import styles from "../_ui/ui.module.css";

const TOKEN_KEY = "reset-link";
const PASSWORD_CODES = ["password_too_short", "password_too_long", "password_too_common"] as const;

export function ResetPassword() {
  const t = useTranslations("ResetPassword");
  const form = useTranslations("Form");
  const token = useLinkToken(TOKEN_KEY);
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [ended, setEnded] = useState<"done" | "expired" | null>(null);
  const [passwordError, setPasswordError] = useState<string>();
  const [message, setMessage] = useState<{ tone: "error" | "note"; text: string } | null>(null);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (busy || !token) return;
    setBusy(true);
    const outcome = await send(accountCompletePasswordReset({ body: { token, password } }));
    setBusy(false);
    setPasswordError(undefined);
    setMessage(null);

    const code = PASSWORD_CODES.find((c) => c === outcome.code);
    if (outcome.status === 204 || outcome.status === 400) {
      forgetToken(TOKEN_KEY);
      setEnded(outcome.status === 204 ? "done" : "expired");
    } else if (code) {
      setPasswordError(form(code));
    } else {
      setMessage({ tone: outcome.status === 503 ? "note" : "error", text: form(problem(outcome)) });
    }
  }

  if (token === undefined) {
    // Server-rendered and before the page runs: without JavaScript the link can't be used.
    return (
      <>
        <h1 className={styles.heading}>{t("title")}</h1>
        <NoScript>{form("noScript")}</NoScript>
      </>
    );
  }

  if (ended === "done") {
    return (
      <Outcome icon="done" title={t("doneTitle")} lede={t("doneLede")}>
        <Link className={`${styles.button} ${styles.primary}`} href="/sign-in">
          {t("signIn")}
        </Link>
      </Outcome>
    );
  }

  // Longer than any link we send: mangled on the way, so it can't work either.
  if (token === null || token.length > 100 || ended === "expired") {
    return (
      <Outcome icon="expired" title={t("expiredTitle")} lede={t("expiredLede")}>
        <Link className={`${styles.button} ${styles.primary}`} href="/forgot-password">
          {t("newLink")}
        </Link>
        <p className={styles.aside}>
          <Link href="/sign-in">{t("back")}</Link>
        </p>
      </Outcome>
    );
  }

  return (
    <>
      <h1 className={styles.heading}>{t("title")}</h1>
      <p className={styles.lede}>{t("lede")}</p>
      {message && <Banner tone={message.tone}>{message.text}</Banner>}
      <form className={styles.form} method="post" onSubmit={onSubmit}>
        <PasswordField
          label={form("newPassword")}
          value={password}
          onChange={setPassword}
          autoComplete="new-password"
          error={passwordError}
        />
        <Submit busy={busy} busyLabel={t("saving")}>
          {t("submit")}
        </Submit>
      </form>
    </>
  );
}
