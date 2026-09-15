"use client";

import { useTranslations } from "next-intl";
import { type FormEvent, useRef, useState } from "react";

import { sessionSignIn } from "@/api-client";
import { Link, useRouter } from "@/i18n/navigation";

import { Banner, EmailField, PasswordField, problem, Screen, send, styles, Submit } from "../_ui/parts";

type Message = { tone: "error" | "note"; text: string };

export function SignInForm() {
  const t = useTranslations("SignIn");
  const form = useTranslations("Form");
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<Message | null>(null);
  const [emailError, setEmailError] = useState<string>();
  const passwordInput = useRef<HTMLInputElement>(null);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    const outcome = await send(sessionSignIn({ body: { email, password } }));
    setBusy(false);
    setEmailError(undefined);
    setMessage(null);

    if (outcome.status === 200) {
      router.replace("/");
    } else if (outcome.status === 401) {
      setMessage({ tone: "error", text: t("invalid") });
      setPassword("");
      passwordInput.current?.focus();
    } else if (outcome.status === 422) {
      setEmailError(form("invalidEmail"));
    } else if (outcome.status === 429) {
      setMessage({ tone: "note", text: t("tooMany") });
    } else if (outcome.status === 403) {
      setMessage({ tone: "error", text: t("noBusiness") });
    } else {
      setMessage({ tone: outcome.status === 503 ? "note" : "error", text: form(problem(outcome)) });
    }
  }

  return (
    <Screen>
      <h1 className={styles.heading}>{t("title")}</h1>
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
