"use client";

import { useTranslations } from "next-intl";
import { type FormEvent, useRef, useState } from "react";

import { sessionSignIn } from "@/api-client";
import { Link, useRouter } from "@/i18n/navigation";
import { signInOutcome } from "@/lib/account";

import { Banner, EmailField, NoScript, PasswordField, problem, Screen, send, Submit } from "../_ui/parts";
import { Turnstile } from "../_ui/turnstile";
import styles from "../_ui/ui.module.css";

type Message = { tone: "error" | "note"; text: string };

export function SignInForm({ turnstileSiteKey }: { turnstileSiteKey: string | null }) {
  const t = useTranslations("SignIn");
  const form = useTranslations("Form");
  const router = useRouter();
  const [email, setEmail] = useState("");
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
    const outcome = await send(sessionSignIn({ body: { email, password, turnstile_token: token } }));
    setBusy(false);
    if (token) {
      setToken(null);
      setAttempt((n) => n + 1);
    }
    setEmailError(undefined);
    setMessage(null);

    const meaning = signInOutcome(outcome.status, outcome.code, Boolean(turnstileSiteKey));
    if (meaning === "ok") {
      router.replace("/");
    } else if (meaning === "challenge") {
      setChallenged(true);
      setMessage({ tone: "note", text: t("challenge") });
    } else if (meaning === "invalid") {
      setMessage({ tone: "error", text: t("invalid") });
      setPassword("");
      passwordInput.current?.focus();
    } else if (outcome.status === 422) {
      setEmailError(form("invalidEmail"));
    } else if (meaning === "try_later") {
      setMessage({ tone: "note", text: t(outcome.code === "challenge_required" ? "tryLater" : "tooMany") });
    } else if (outcome.status === 403) {
      setMessage({ tone: "error", text: t("noBusiness") });
    } else {
      setMessage({ tone: outcome.status === 503 ? "note" : "error", text: form(problem(outcome)) });
    }
  }

  return (
    <Screen>
      <h1 className={styles.heading}>{t("title")}</h1>
      <NoScript>{form("needsJavaScript")}</NoScript>
      {message && <Banner tone={message.tone}>{message.text}</Banner>}
      <form className={styles.form} method="post" onSubmit={onSubmit}>
        <EmailField value={email} onChange={setEmail} error={emailError} />
        <PasswordField
          label={form("password")}
          value={password}
          onChange={setPassword}
          autoComplete="current-password"
          inputRef={passwordInput}
          labelAside={<Link href="/forgot-password">{t("forgot")}</Link>}
        />
        {challenged && turnstileSiteKey && <Turnstile key={attempt} siteKey={turnstileSiteKey} action="sign_in" onToken={setToken} />}
        <Submit busy={busy} busyLabel={t("signingIn")}>
          {t("submit")}
        </Submit>
      </form>
      <p className={styles.aside}>
        {t("newHere")} <Link href="/sign-up">{t("createBusiness")}</Link>
      </p>
    </Screen>
  );
}
