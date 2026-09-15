"use client";

import { useLocale, useTranslations } from "next-intl";
import { type FormEvent, type ReactNode, useState } from "react";

import { accountRequestPasswordReset, accountSignUp } from "@/api-client";

import { Banner, CheckInbox, EmailField, problem, send, styles, Submit } from "./parts";

type Props = {
  purpose: "sign_up" | "password_reset";
  /** What sits above the email field: a heading, and a mark or a lede. */
  intro: ReactNode;
  submit: string;
  aside?: ReactNode;
};

/** Ask for an emailed link, then "check your inbox" with a 60-second resend countdown. */
export function LinkRequest({ purpose, intro, submit, aside }: Props) {
  const form = useTranslations("Form");
  const tooMany = useTranslations(purpose === "sign_up" ? "SignUp" : "ForgotPassword")("tooMany");
  const locale = useLocale() as "en" | "nl" | "pt";
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [sentAt, setSentAt] = useState<number | null>(null);
  const [emailError, setEmailError] = useState<string>();
  const [message, setMessage] = useState<{ tone: "error" | "note"; text: string } | null>(null);

  async function request(): Promise<number | null> {
    const call = purpose === "sign_up" ? accountSignUp : accountRequestPasswordReset;
    const outcome = await send(call({ body: { email, locale } }));
    setEmailError(undefined);
    setMessage(null);
    if (outcome.status === 202) {
      const now = Date.now();
      setSentAt(now);
      return now;
    }
    if (outcome.status === 422) setEmailError(form("invalidEmail"));
    else if (outcome.status === 429) setMessage({ tone: "note", text: tooMany });
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
        error={message && <Banner tone={message.tone}>{message.text}</Banner>}
      />
    );
  }

  return (
    <>
      {intro}
      {message && <Banner tone={message.tone}>{message.text}</Banner>}
      <form className={styles.form} method="post" onSubmit={onSubmit}>
        <EmailField value={email} onChange={setEmail} error={emailError} />
        <Submit busy={busy} busyLabel={form("sending")}>
          {submit}
        </Submit>
      </form>
      {aside}
    </>
  );
}
