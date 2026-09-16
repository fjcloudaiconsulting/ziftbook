"use client";

import { useLocale, useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { accountCompleteSignUp } from "@/api-client";
import { Link, useRouter } from "@/i18n/navigation";
import { byName, type Country, likelyCountry } from "@/lib/account";

import { LinkRequest } from "../../_ui/link-request";
import { Banner, FieldError, forgetToken, Heading, Mark, NoScript, Outcome, PasswordField, problem, send, Submit, useLinkToken } from "../../_ui/parts";
import styles from "../../_ui/ui.module.css";

const TOKEN_KEY = "sign-up-link";
const PASSWORD_CODES = ["password_too_short", "password_too_long", "password_too_common"] as const;

export function CompleteSignUp() {
  const t = useTranslations("CompleteSignUp");
  const form = useTranslations("Form");
  const router = useRouter();
  const token = useLinkToken(TOKEN_KEY);
  const locale = useLocale();
  const nameId = useId();
  const countryId = useId();
  const [name, setName] = useState("");
  const [country, setCountry] = useState<Country | "">(() => likelyCountry(locale));
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [ended, setEnded] = useState<"expired" | "exists" | null>(null);
  const [passwordError, setPasswordError] = useState<string>();
  const [nameError, setNameError] = useState<string>();
  const [countryError, setCountryError] = useState<string>();
  const [message, setMessage] = useState<{ tone: "error" | "note"; text: string } | null>(null);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (busy || !token) return;
    // The server strips spaces too; a blank name would only come back as a vague "invalid request".
    setNameError(name.trim() ? undefined : t("nameRequired"));
    setCountryError(country ? undefined : t("countryRequired"));
    if (!name.trim() || !country) return;
    setBusy(true);
    const outcome = await send(accountCompleteSignUp({ body: { token, password, business_name: name, country } }));
    setBusy(false);
    setPasswordError(undefined);
    setMessage(null);

    const code = PASSWORD_CODES.find((c) => c === outcome.code);
    if (outcome.status === 201) {
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
        <NoScript>{form("noScript")}</NoScript>
      </>
    );
  }

  // Longer than any link we send: mangled on the way, so it can't work either.
  if (token === null || token.length > 100 || ended === "expired") {
    return (
      <LinkRequest
        purpose="sign_up"
        intro={
          <>
            <Mark icon="expired" />
            <Heading focus>{t("expiredTitle")}</Heading>
            <p className={styles.lede}>{t("expiredLede")}</p>
          </>
        }
        submit={t("newLink")}
      />
    );
  }

  if (ended === "exists") {
    return (
      <Outcome icon="person" title={t("existsTitle")} lede={t("existsLede")}>
        <Link className={`${styles.button} ${styles.primary}`} href="/sign-in">
          {t("signIn")}
        </Link>
        <p className={styles.aside}>
          <Link href="/forgot-password">{t("forgot")}</Link>
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
              aria-invalid={nameError ? true : undefined}
              aria-describedby={`${nameId}-hint`}
            />
          </div>
          {nameError ? (
            <FieldError id={`${nameId}-hint`}>{nameError}</FieldError>
          ) : (
            <p className={styles.hint} id={`${nameId}-hint`}>
              {t("businessHint")}
            </p>
          )}
        </div>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={countryId}>
            {t("country")}
          </label>
          <div className={styles.input}>
            <select
              id={countryId}
              name="country"
              autoComplete="country"
              required
              value={country}
              onChange={(event) => setCountry(event.target.value as Country)}
              aria-invalid={countryError ? true : undefined}
              aria-describedby={`${countryId}-hint`}
            >
              <option value="" disabled>
                {t("countryPlaceholder")}
              </option>
              {byName(locale, (c) => t(`countries.${c}`)).map((c) => (
                <option key={c} value={c}>
                  {t(`countries.${c}`)}
                </option>
              ))}
            </select>
            <svg aria-hidden="true" className={styles.selectChevron} viewBox="0 0 12 12" width="12" height="12">
              <path d="M3 4.5l3 3 3-3" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </div>
          {countryError ? (
            <FieldError id={`${countryId}-hint`}>{countryError}</FieldError>
          ) : (
            <p className={styles.hint} id={`${countryId}-hint`}>
              {t("countryHint")}
            </p>
          )}
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
