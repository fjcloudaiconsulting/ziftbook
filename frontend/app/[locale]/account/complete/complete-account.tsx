"use client";

import { useLocale, useTranslations } from "next-intl";
import { type FormEvent, useId, useMemo, useState } from "react";

import { platformAccountCompleteSignUp } from "@/api-client";
import { Link } from "@/i18n/navigation";
import { trimmedName } from "@/lib/account";
import { accountPhone, completeOutcome, dialRegion, takeReturn } from "@/lib/client-account";
import { dialOptions } from "@/lib/dial-codes";
import { type OpenedLink } from "@/lib/link";

import { LinkRequest } from "../../_ui/link-request";
import { Banner, FieldError, forgetLink, Heading, Mark, NoScript, Outcome, PasswordField, problem, send, Submit, useLink } from "../../_ui/parts";
import styles from "../../_ui/ui.module.css";
import { DialPicker } from "../../[slug]/dial-picker";

const TOKEN_KEY = "account-sign-up-link";

export function CompleteAccount({ turnstileSiteKey }: { turnstileSiteKey: string | null }) {
  const link = useLink(TOKEN_KEY);
  const t = useTranslations("ClientSignUp");
  const form = useTranslations("Form");

  if (link === undefined) {
    // Server-rendered and before the page runs: without JavaScript the link can't be used.
    return (
      <>
        <h1 className={styles.heading}>{t("completeTitle")}</h1>
        <NoScript>{form("noScript")}</NoScript>
      </>
    );
  }

  // Every opening of a link starts over: the old link's screen doesn't outlive it.
  return <AccountForm key={link?.opened} link={link} turnstileSiteKey={turnstileSiteKey} />;
}

function AccountForm({ link, turnstileSiteKey }: { link: OpenedLink | null; turnstileSiteKey: string | null }) {
  const token = link?.token ?? null;
  const t = useTranslations("ClientSignUp");
  const done = useTranslations("CompleteSignUp");
  const form = useTranslations("Form");
  const locale = useLocale();
  const nameId = useId();
  const phoneId = useId();
  const [name, setName] = useState("");
  // Only ever rendered in the browser (the link is read there), so the browser's own languages are safe to read.
  const [region, setRegion] = useState(() => dialRegion(navigator.languages ?? [], locale));
  const dials = useMemo(() => dialOptions(region, locale), [region, locale]);
  const [phone, setPhone] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [ended, setEnded] = useState<"expired" | "exists" | null>(null);
  // G3: the address is shown once the account exists (the answer carries it); nothing before reads a token's email.
  const [created, setCreated] = useState<string | null>(null);
  // The booking page the sign-up started from, when it started in this browser (owner ruling 7).
  const [slug, setSlug] = useState<string | null>(null);
  const [nameError, setNameError] = useState<string>();
  const [phoneError, setPhoneError] = useState<string>();
  const [passwordError, setPasswordError] = useState<string>();
  const [message, setMessage] = useState<{ tone: "error" | "note"; text: string } | null>(null);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (busy || !token) return;
    const person = trimmedName(name);
    setNameError(person ? undefined : t("nameRequired"));
    if (!person) return;
    setBusy(true);
    const outcome = await send(platformAccountCompleteSignUp({ body: { token, password, name: person, phone: accountPhone(region, phone) } }));
    setBusy(false);
    setPasswordError(undefined);
    setPhoneError(undefined);
    setMessage(null);

    const meaning = completeOutcome(outcome.status, outcome.code);
    if (meaning === "done" && outcome.data) {
      forgetLink(TOKEN_KEY, link);
      setPassword("");
      setSlug(takeReturn(() => window.localStorage, Date.now()));
      setCreated(outcome.data.email);
    } else if (meaning === "expired" || meaning === "exists") {
      forgetLink(TOKEN_KEY, link);
      setEnded(meaning);
    } else if (meaning === "password" && outcome.code) {
      setPasswordError(form(outcome.code as "password_too_short" | "password_too_long" | "password_too_common"));
    } else if (meaning === "phone") {
      setPhoneError(t("phoneInvalid"));
    } else {
      setMessage({ tone: outcome.status === 503 ? "note" : "error", text: form(problem(outcome)) });
    }
  }

  if (created !== null) {
    return (
      <Outcome icon="done" title={t("doneTitle")} lede={t.rich("doneLede", { email: created, chip: (chunks) => <span className={styles.chip}>{chunks}</span> })}>
        {slug && (
          <Link className={`${styles.button} ${styles.primary}`} href={`/${slug}`}>
            {t("backToBooking")}
          </Link>
        )}
        <Link className={`${styles.button} ${slug ? styles.secondary : styles.primary}`} href="/account">
          {t("yourBookings")}
        </Link>
      </Outcome>
    );
  }

  if (ended === "exists") {
    return (
      <Outcome icon="person" title={done("existsTitle")} lede={t("existsLede")}>
        <Link className={`${styles.button} ${styles.primary}`} href="/account/sign-in">
          {done("signIn")}
        </Link>
        <p className={styles.aside}>
          <Link href="/forgot-password">{done("forgot")}</Link>
        </p>
      </Outcome>
    );
  }

  // Longer than any link we send: mangled on the way, so it can't work either.
  if (token === null || token.length > 100 || ended === "expired") {
    return (
      <LinkRequest
        purpose="account_sign_up"
        turnstileSiteKey={turnstileSiteKey}
        intro={
          <>
            <Mark icon="expired" />
            <Heading focus>{done("expiredTitle")}</Heading>
            <p className={styles.lede}>{done("expiredLede")}</p>
          </>
        }
        submit={done("newLink")}
      />
    );
  }

  return (
    <>
      <h1 className={styles.heading}>{t("completeTitle")}</h1>
      <p className={styles.lede}>{t("completeLede")}</p>
      {message && <Banner tone={message.tone}>{message.text}</Banner>}
      <form className={styles.form} method="post" onSubmit={onSubmit}>
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
              aria-invalid={nameError ? true : undefined}
              aria-describedby={`${nameId}-hint`}
            />
          </div>
          {nameError ? (
            <FieldError id={`${nameId}-hint`}>{nameError}</FieldError>
          ) : (
            <p className={styles.hint} id={`${nameId}-hint`}>
              {t("nameHint")}
            </p>
          )}
        </div>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={phoneId}>
            {t("phone")} <span className={styles.optional}>{t("phoneOptional")}</span>
          </label>
          <div className={styles.phonePair}>
            <DialPicker options={dials} value={region} onChange={setRegion} />
            <div className={styles.input}>
              <input
                id={phoneId}
                name="phone"
                type="tel"
                autoComplete="tel-national"
                maxLength={36}
                value={phone}
                onChange={(event) => setPhone(event.target.value)}
                aria-invalid={phoneError ? true : undefined}
                aria-describedby={`${phoneId}-hint`}
              />
            </div>
          </div>
          {phoneError ? (
            <FieldError id={`${phoneId}-hint`}>{phoneError}</FieldError>
          ) : (
            <p className={styles.hint} id={`${phoneId}-hint`}>
              {t("phoneHint")}
            </p>
          )}
        </div>
        <PasswordField label={form("password")} value={password} onChange={setPassword} autoComplete="new-password" error={passwordError} />
        <Submit busy={busy} busyLabel={done("creating")}>
          {t("submit")}
        </Submit>
      </form>
    </>
  );
}
