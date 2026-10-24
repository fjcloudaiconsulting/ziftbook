"use client";

import { useLocale, useTranslations } from "next-intl";
import { type FormEvent, type ReactNode, useState } from "react";

import { accountRequestPasswordReset, accountSignUp } from "@/api-client";

import { Banner, CheckInbox, EmailField, NoScript, problem, send, Submit } from "./parts";
import { Turnstile } from "./turnstile";
import styles from "./ui.module.css";

type Props = {
  purpose: "sign_up" | "password_reset";
  /** What sits above the email field: a heading, and a mark or a lede. */
  intro: ReactNode;
  submit: string;
  aside?: ReactNode;
  /** Cloudflare Turnstile's public site key; null when the check is off, and then no token is sent. */
  turnstileSiteKey: string | null;
};

/** Ask for an emailed link, then "check your inbox" with a 60-second resend countdown. */
export function LinkRequest({ purpose, intro, submit, aside, turnstileSiteKey }: Props) {
  const form = useTranslations("Form");
  const tooMany = useTranslations(purpose === "sign_up" ? "SignUp" : "ForgotPassword")("tooMany");
  const locale = useLocale() as "en" | "nl" | "pt";
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [sentAt, setSentAt] = useState<number | null>(null);
  const [emailError, setEmailError] = useState<string>();
  const [message, setMessage] = useState<{ tone: "error" | "note"; text: string } | null>(null);
  // A token is good for one request: each request uses it up and brings a fresh widget.
  const [token, setToken] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const widget = turnstileSiteKey && (
    <Turnstile key={attempt} siteKey={turnstileSiteKey} action={purpose} onToken={setToken} />
  );

  async function request(): Promise<number | null> {
    const call = purpose === "sign_up" ? accountSignUp : accountRequestPasswordReset;
    const outcome = await send(call({ body: { email, locale, turnstile_token: token } }));
    if (token) {
      setToken(null);
      setAttempt((n) => n + 1);
    }
    setEmailError(undefined);
    setMessage(null);
    if (outcome.status === 202) {
      const now = Date.now();
      setSentAt(now);
      return now;
    }
    if (outcome.status === 422) setEmailError(form("invalidEmail"));
    else if (outcome.status === 429) setMessage({ tone: "note", text: tooMany });
    else if (outcome.status === 403 && outcome.code === "turnstile_failed") setMessage({ tone: "error", text: form("verifyFailed") });
    else setMessage({ tone: "error", text: form(problem(outcome)) });
    return null;
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    await request();
    setBusy(false);
  }

  if (sentAt !== null && !emailError) {
    return (
      <CheckInbox
        email={email}
        lede={purpose === "sign_up" ? "signUpLede" : "resetLede"}
        sentAt={sentAt}
        resend={request}
        differentEmail={() => {
          setSentAt(null);
          setMessage(null);
        }}
        widget={widget}
        error={message && <Banner tone={message.tone}>{message.text}</Banner>}
      />
    );
  }

  return (
    <>
      {intro}
      <NoScript>{form("needsJavaScript")}</NoScript>
      {message && <Banner tone={message.tone}>{message.text}</Banner>}
      <form className={styles.form} method="post" onSubmit={onSubmit}>
        <EmailField value={email} onChange={setEmail} error={emailError} />
        {widget}
        <Submit busy={busy} busyLabel={form("sending")}>
          {submit}
        </Submit>
      </form>
      {aside}
    </>
  );
}
