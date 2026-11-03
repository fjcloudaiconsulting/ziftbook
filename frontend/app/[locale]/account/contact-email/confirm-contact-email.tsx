"use client";

import { useTranslations } from "next-intl";
import { useState, useSyncExternalStore } from "react";

import { contactEmailConfirm } from "@/api-client";
import { takeToken } from "@/lib/account";
import { confirmOutcome, firstStage } from "@/lib/contact-email";
import { linkStore, type OpenedLink } from "@/lib/link";

import { Banner, Heading, Mark, NoScript, Outcome, problem, send } from "../../_ui/parts";
import styles from "../../_ui/ui.module.css";

const store = linkStore(() => takeToken(window), globalThis);

/** The link that opened this page, or the one opened since; undefined on the server and while hydrating. */
function useConfirmLink(): OpenedLink | null | undefined {
  return useSyncExternalStore(store.subscribe, store.snapshot, () => undefined);
}

export function ConfirmContactEmail() {
  const link = useConfirmLink();
  const t = useTranslations("ContactEmail");
  const form = useTranslations("Form");

  if (link === undefined) {
    // Server-rendered and before the page runs: without JavaScript the link can't be used.
    return (
      <>
        <h1 className={styles.heading}>{t("title")}</h1>
        <NoScript>{form("noScript")}</NoScript>
      </>
    );
  }
  // Keyed: every link opened in this tab starts over.
  return <Confirm key={link?.opened ?? -1} link={link} />;
}

function Confirm({ link }: { link: OpenedLink | null }) {
  const token = link?.token ?? null;
  const t = useTranslations("ContactEmail");
  const form = useTranslations("Form");
  const [stage, setStage] = useState(() => firstStage(token));
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [message, setMessage] = useState<{ tone: "error" | "note"; text: string } | null>(null);

  // Only a press confirms, never the page loading: a mail scanner that runs scripts confirms nothing.
  async function onConfirm() {
    if (busy || !token) return;
    setBusy(true);
    setMessage(null);
    const outcome = await send(contactEmailConfirm({ body: { token } }));
    setBusy(false);
    const meaning = confirmOutcome(outcome.status);
    if (meaning === "done" || meaning === "invalid") store.forget(link);
    if (meaning === "done") setDone(true);
    else if (meaning === "invalid") setStage("invalid");
    else if (meaning === "tooMany") setMessage({ tone: "note", text: t("tooMany") });
    else setMessage({ tone: outcome.status === 503 ? "note" : "error", text: form(problem(outcome)) });
  }

  if (done) {
    return (
      <Outcome icon="done" title={t("doneTitle")} lede={t("done")}>
        {null}
      </Outcome>
    );
  }
  if (stage === "invalid") {
    return (
      <Outcome icon="expired" title={t("invalidTitle")} lede={t("invalid")}>
        {null}
      </Outcome>
    );
  }
  if (stage === "missing") {
    return (
      <>
        <h1 className={styles.heading}>{t("title")}</h1>
        <Banner tone="info">{t("missing")}</Banner>
      </>
    );
  }
  return (
    <>
      <Mark icon="mail" />
      <Heading>{t("title")}</Heading>
      <p className={styles.lede}>{t("intro")}</p>
      {message && <Banner tone={message.tone}>{message.text}</Banner>}
      <button className={`${styles.button} ${styles.primary}`} type="button" aria-disabled={busy || undefined} onClick={onConfirm}>
        {busy && <span className={styles.spinner} aria-hidden="true" />}
        {busy ? t("confirming") : t("confirm")}
      </button>
    </>
  );
}
