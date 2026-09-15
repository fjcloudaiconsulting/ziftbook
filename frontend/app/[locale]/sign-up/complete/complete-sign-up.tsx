"use client";

import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { accountCompleteSignUp } from "@/api-client";
import { Link, useRouter } from "@/i18n/navigation";

import { LinkRequest } from "../../_ui/link-request";
import { Banner, forgetToken, Mark, PasswordField, problem, send, styles, Submit, useLinkToken } from "../../_ui/parts";

const TOKEN_KEY = "sign-up-link";
const PASSWORD_CODES = ["password_too_short", "password_too_long", "password_too_common"] as const;

export function CompleteSignUp() {
  const t = useTranslations("CompleteSignUp");
  const form = useTranslations("Form");
  const router = useRouter();
  const token = useLinkToken(TOKEN_KEY);
  const nameId = useId();
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [ended, setEnded] = useState<"expired" | "exists" | null>(null);
  const [passwordError, setPasswordError] = useState<string>();
  const [message, setMessage] = useState<{ tone: "error" | "note"; text: string } | null>(null);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (busy || !token) return;
    setBusy(true);
    const outcome = await send(accountCompleteSignUp({ body: { token, password, business_name: name } }));
    setBusy(false);
    setPasswordError(undefined);
    setMessage(null);

    const code = PASSWORD_CODES.find((c) => c === outcome.code);
    if (outcome.status === 201) {
      forgetToken(TOKEN_KEY);
      router.replace("/");
    } else if (outcome.status === 400 || outcome.status === 409) {
      forgetToken(TOKEN_KEY);
      setEnded(outcome.status === 400 ? "expired" : "exists");
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
        <noscript>
          <Banner tone="info">{form("noScript")}</Banner>
        </noscript>
      </>
    );
  }

  if (token === null || ended === "expired") {
    return (
      <LinkRequest
        purpose="sign_up"
        intro={
          <>
            <Mark icon="expired" />
            <h1 className={styles.heading}>{t("expiredTitle")}</h1>
            <p className={styles.lede}>{t("expiredLede")}</p>
          </>
        }
        submit={t("newLink")}
      />
    );
  }

  if (ended === "exists") {
    return (
      <>
        <Mark icon="person" />
        <h1 className={styles.heading}>{t("existsTitle")}</h1>
        <p className={styles.lede}>{t("existsLede")}</p>
        <div className={styles.stack}>
          <Link className={`${styles.button} ${styles.primary}`} href="/sign-in">
            {t("signIn")}
          </Link>
          <p className={styles.aside}>
            <Link href="/forgot-password">{t("forgot")}</Link>
          </p>
        </div>
      </>
    );
  }

  return (
    <>
      <h1 className={styles.heading}>{t("title")}</h1>
      <p className={styles.lede}>{t("lede")}</p>
      {message && <Banner tone={message.tone}>{message.text}</Banner>}
      <form className={styles.form} method="post" onSubmit={onSubmit}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={nameId}>
            {t("businessName")}
          </label>
          <div className={styles.input}>
            <input
              id={nameId}
              name="organization"
              autoComplete="organization"
              required
              maxLength={100}
              value={name}
              onChange={(event) => setName(event.target.value)}
              aria-describedby={`${nameId}-hint`}
            />
          </div>
          <p className={styles.hint} id={`${nameId}-hint`}>
            {t("businessHint")}
          </p>
        </div>
        <PasswordField
          label={form("password")}
          value={password}
          onChange={setPassword}
          autoComplete="new-password"
          error={passwordError}
        />
        <Submit busy={busy} busyLabel={t("creating")}>
          {t("submit")}
        </Submit>
      </form>
    </>
  );
}
