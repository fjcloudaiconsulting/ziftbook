"use client";

import { useLocale, useTranslations } from "next-intl";
import { type FormEvent, type ReactNode, useRef, useState } from "react";

import { type AccountOut, platformAccountSignIn } from "@/api-client";
import { Link } from "@/i18n/navigation";
import { accountSignInOutcome } from "@/lib/client-account";

import { Banner, EmailField, PasswordField, problem, send, Submit } from "./parts";
import { Turnstile } from "./turnstile";
import styles from "./ui.module.css";

type Message = { tone: "error" | "note"; text: string };

/**
 * ZIF-146: signing in to a client's own ziftbook account (`POST /api/account/session`), on the account sign-in page
 * and in the checkout's dialog. It starts no console session. The answers are the console sign-in's: a wrong
 * password is `SignIn.invalid`, which never says whether the email has an account.
 */
export function AccountSignIn({
  turnstileSiteKey,
  onSignedIn,
  intro,
  aside,
  forgotNewTab = false,
  initialEmail = "",
}: {
  turnstileSiteKey: string | null;
  onSignedIn(account: AccountOut): void;
  intro?: ReactNode;
  aside?: ReactNode;
  /** In the checkout's dialog: the reset page opens beside it, so nothing typed into the booking is lost. */
  forgotNewTab?: boolean;
  /** Asking a signed-in person for their password again (history needs a session started within a day). */
  initialEmail?: string;
}) {
  const t = useTranslations("SignIn");
  const form = useTranslations("Form");
  const client = useTranslations("ClientSignUp");
  const locale = useLocale();
  const [email, setEmail] = useState(initialEmail);
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<Message | null>(null);
  const [emailError, setEmailError] = useState<string>();
  const passwordInput = useRef<HTMLInputElement>(null);
  // Set once the server asks for a challenge; each attempt with a token uses it up and brings a fresh widget.
  const [challenged, setChallenged] = useState(false);
  const [token, setToken] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    const outcome = await send(platformAccountSignIn({ body: { email, password, turnstile_token: token } }));
    setBusy(false);
    if (token) {
      setToken(null);
      setAttempt((n) => n + 1);
    }
    setEmailError(undefined);
    setMessage(null);

    const meaning = accountSignInOutcome(outcome.status, outcome.code, Boolean(turnstileSiteKey));
    if (meaning === "ok" && outcome.data) {
      setPassword("");
      onSignedIn(outcome.data);
    } else if (meaning === "challenge") {
      setChallenged(true);
      setMessage({ tone: "note", text: t("challenge") });
    } else if (meaning === "invalid") {
      setMessage({ tone: "error", text: t("invalid") });
      setPassword("");
      passwordInput.current?.focus();
    } else if (meaning === "unavailable") {
      setMessage({ tone: "error", text: t("accountUnavailable") });
    } else if (outcome.status === 422) {
      setEmailError(form("invalidEmail"));
    } else if (meaning === "try_later") {
      setMessage({ tone: "note", text: t(outcome.code === "challenge_required" ? "tryLater" : "tooMany") });
    } else {
      setMessage({ tone: outcome.status === 503 ? "note" : "error", text: form(problem(outcome)) });
    }
  }

  const forgot = forgotNewTab ? (
    <a href={`/${locale}/forgot-password`} target="_blank" rel="noopener">
      {t("forgot")}
    </a>
  ) : (
    <Link href="/forgot-password">{t("forgot")}</Link>
  );

  return (
    <>
      {intro}
      {message && <Banner tone={message.tone}>{message.text}</Banner>}
      <form className={styles.form} method="post" onSubmit={onSubmit}>
        <EmailField value={email} onChange={setEmail} error={emailError} placeholder={client("emailPlaceholder")} />
        <PasswordField
          label={form("password")}
          value={password}
          onChange={setPassword}
          autoComplete="current-password"
          inputRef={passwordInput}
          labelAside={forgot}
        />
        {challenged && turnstileSiteKey && <Turnstile key={attempt} siteKey={turnstileSiteKey} action="sign_in" onToken={setToken} />}
        <Submit busy={busy} busyLabel={t("signingIn")}>
          {t("submit")}
        </Submit>
      </form>
      {aside}
    </>
  );
}
