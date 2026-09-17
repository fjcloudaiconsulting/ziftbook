"use client";

import { useTranslations } from "next-intl";
import { type FormEvent, useCallback, useEffect, useId, useRef, useState } from "react";

import { type InviteDetails, invitesAccept, invitesLookup } from "@/api-client";
import { Link, useRouter } from "@/i18n/navigation";
import { acceptBody, acceptOutcome, inviteScreen, isInviteToken } from "@/lib/invite";

import { Banner, forgetToken, NoScript, Outcome, PasswordField, problem, send, Submit, useLinkToken } from "../_ui/parts";
import styles from "../_ui/ui.module.css";

const TOKEN_KEY = "invite-link";

type Message = { tone: "error" | "note" | "info"; text: string };
type Stage =
  | { is: "checking" }
  | { is: "retry"; message: Message }
  | { is: "form"; invite: InviteDetails }
  | { is: "expired" }
  | { is: "member"; business: string };

export function AcceptInvite() {
  const token = useLinkToken(TOKEN_KEY);
  const t = useTranslations("Invite");
  const form = useTranslations("Form");

  if (token === undefined) {
    // Server-rendered and before the page runs: without JavaScript the link can't be used.
    return (
      <>
        <h1 className={styles.heading}>{t("title")}</h1>
        <NoScript>{form("noScript")}</NoScript>
      </>
    );
  }
  // Keyed: a new link pasted into the same tab starts over.
  return <Invite key={token ?? ""} token={token && isInviteToken(token) ? token : null} />;
}

function Invite({ token }: { token: string | null }) {
  const t = useTranslations("Invite");
  const form = useTranslations("Form");
  const signIn = useTranslations("SignIn");
  const router = useRouter();
  const emailId = useId();
  const passwordInput = useRef<HTMLInputElement>(null);
  const [stage, setStage] = useState<Stage>(token ? { is: "checking" } : { is: "expired" });
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [passwordError, setPasswordError] = useState<string>();
  const [message, setMessage] = useState<Message | null>(null);

  const problemText = useCallback(
    (outcome: { status: number }): Message =>
      outcome.status === 429
        ? { tone: "note", text: t("tooMany") }
        : { tone: outcome.status === 503 ? "note" : "error", text: form(problem(outcome)) },
    [t, form],
  );

  const lookUp = useCallback(() => {
    if (!token) return;
    send(invitesLookup({ body: { token } })).then((outcome) => {
      const screen = inviteScreen(outcome);
      if (screen === "expired") forgetToken(TOKEN_KEY);
      setStage(
        screen === "new" || screen === "existing"
          ? { is: "form", invite: outcome.data! }
          : screen === "expired"
            ? { is: "expired" }
            : { is: "retry", message: problemText(outcome) },
      );
    });
  }, [token, problemText]);

  useEffect(() => {
    lookUp();
  }, [lookUp]);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (busy || !token || stage.is !== "form") return;
    const invite = stage.invite;
    setBusy(true);
    const outcome = await send(invitesAccept({ body: acceptBody(token, password) }));
    setBusy(false);
    setPasswordError(undefined);
    setMessage(null);

    const meaning = acceptOutcome(outcome);
    if (meaning === "joined") {
      forgetToken(TOKEN_KEY);
      router.replace("/");
    } else if (meaning === "expired") {
      forgetToken(TOKEN_KEY);
      setStage({ is: "expired" });
    } else if (meaning === "alreadyMember") {
      forgetToken(TOKEN_KEY);
      setStage({ is: "member", business: invite.business_name });
    } else if (meaning === "wrongPassword") {
      setMessage({ tone: "error", text: signIn("invalid") });
      setPassword("");
      passwordInput.current?.focus();
    } else if (meaning === "accountExists") {
      // Someone made the account after the page loaded: from now on it's that account's password.
      setStage({ is: "form", invite: { ...invite, has_account: true } });
      setMessage({ tone: "info", text: t("accountExists") });
      setPassword("");
      passwordInput.current?.focus();
    } else if (meaning.startsWith("password_")) {
      setPasswordError(form(meaning as "password_too_short" | "password_too_long" | "password_too_common"));
    } else {
      setMessage(problemText(outcome));
    }
  }

  if (stage.is === "expired") {
    return (
      <Outcome icon="expired" title={t("expiredTitle")} lede={t("expiredLede")}>
        <p className={styles.aside}>
          {t("joinedBefore")} <Link href="/sign-in">{t("signIn")}</Link>
        </p>
      </Outcome>
    );
  }

  if (stage.is === "member") {
    return (
      <Outcome icon="person" title={t("memberTitle", { business: stage.business })} lede={t("memberLede")}>
        <Link className={`${styles.button} ${styles.primary}`} href="/sign-in">
          {t("signIn")}
        </Link>
      </Outcome>
    );
  }

  if (stage.is !== "form") {
    return (
      <>
        <h1 className={styles.heading}>{t("title")}</h1>
        {stage.is === "checking" ? (
          <p className={styles.lede} role="status">
            {t("checking")}
          </p>
        ) : (
          <>
            <Banner tone={stage.message.tone}>{stage.message.text}</Banner>
            <button
              className={`${styles.button} ${styles.secondary}`}
              type="button"
              onClick={() => {
                setStage({ is: "checking" });
                lookUp();
              }}
            >
              {t("tryAgain")}
            </button>
          </>
        )}
      </>
    );
  }

  const { invite } = stage;
  const existing = invite.has_account;
  return (
    <>
      {/* The business name is text someone typed: only ever rendered as text. */}
      <h1 className={styles.heading}>{t("joinTitle", { business: invite.business_name })}</h1>
      <p className={styles.lede}>{existing ? t("existingLede") : t("newLede")}</p>
      {message && <Banner tone={message.tone}>{message.text}</Banner>}
      <form className={styles.form} method="post" onSubmit={onSubmit}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={emailId}>
            {form("email")}
          </label>
          <div className={styles.input}>
            {/* Read-only, and there for password managers to file the password under. */}
            <input id={emailId} name="email" type="email" autoComplete="username" readOnly value={invite.email} />
          </div>
        </div>
        <PasswordField
          label={form("password")}
          value={password}
          onChange={setPassword}
          autoComplete={existing ? "current-password" : "new-password"}
          error={passwordError}
          inputRef={passwordInput}
          labelAside={existing ? <Link href="/forgot-password">{signIn("forgot")}</Link> : undefined}
        />
        <Submit busy={busy} busyLabel={t("joining")}>
          {existing ? t("signInSubmit") : t("newSubmit")}
        </Submit>
      </form>
    </>
  );
}
