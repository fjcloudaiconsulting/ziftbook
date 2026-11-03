"use client";

import { useTranslations } from "next-intl";
import { type ReactNode, useEffect, useRef, useState } from "react";

import { type ContactEmailOut, contactEmailCancel, contactEmailRead, contactEmailResend } from "@/api-client";
import { contactActionOutcome, contactEmailView, contactLoadOutcome } from "@/lib/settings";

import { JSON_WRITE, SignedOutBanner, useConsole } from "../../_ui/console";
import styles from "../../_ui/console.module.css";
import { Banner } from "../../_ui/parts";
import uiStyles from "../../_ui/ui.module.css";

type Problem = "contactTooMany" | "contactTooManyToday" | "contactOwnerOnly" | "contactFailed";

/** Whether the business's contact email is confirmed (ZIF-232): the address clients see, the one waiting for its
 * inbox to confirm it, and (owners only) the button that mails it a link. Nothing when there is no address. */
export function ContactEmail() {
  const { session, call } = useConsole();
  const t = useTranslations("Console.settings");
  const [state, setState] = useState<ContactEmailOut | null>(null);
  const [loaded, setLoaded] = useState<"show" | "hide" | "failed" | null>(null);
  const [busy, setBusy] = useState<"send" | "cancel" | null>(null);
  const [problem, setProblem] = useState<Problem | null>(null);
  const [announced, setAnnounced] = useState("");
  const [signedOut, setSignedOut] = useState(false);
  const busyRef = useRef(false);

  // Bumped to read the state again (a send found nothing waiting any more).
  const [reads, setReads] = useState(0);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const outcome = await call(() => contactEmailRead());
      if (cancelled) return;
      const shown = contactLoadOutcome(outcome.status === 200 && !outcome.data ? 0 : outcome.status);
      if (shown === "show") setState(outcome.data!);
      setLoaded(shown);
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- on load, and when asked to read again
  }, [reads]);

  async function act(kind: "send" | "cancel") {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(kind);
    setProblem(null);
    setSignedOut(false);
    setAnnounced("");
    const outcome = await call(
      () => (kind === "send" ? contactEmailResend(JSON_WRITE) : contactEmailCancel(JSON_WRITE)),
      { write: true },
    );
    busyRef.current = false;
    setBusy(null);
    const meaning = contactActionOutcome(outcome.status, outcome.code);
    if (meaning === "done" && outcome.data) {
      setState(outcome.data);
      setAnnounced(t(kind === "send" ? "contactLinkSent" : "contactCancelled"));
    } else if (meaning === "signedOut") setSignedOut(true);
    else if (meaning === "reload") setReads((n) => n + 1);
    else if (meaning !== "done") setProblem(meaning);
  }

  // Outside the block, so an announcement survives the block going away (a cancel with nothing
  // confirmed leaves no address to show).
  const live = (
    <p className={uiStyles.srOnly} role="status" aria-live="polite">
      {announced}
    </p>
  );
  const view = state && contactEmailView(state, session.role === "owner");
  let content: ReactNode = null;
  if (!state) {
    if (loaded === "failed") content = <Banner tone="error">{t("contactLoadFailed")}</Banner>;
  } else if (view) {
    const [bodyKey, bodyValues] = view.body;
    const confirmed = view.pill === "confirmed";
    content = (
      <div className={styles.address}>
        <span className={uiStyles.label}>{t("contactEmailLabel")}</span>
        {signedOut && <SignedOutBanner />}
        <span className={`${styles.statusPill} ${confirmed ? styles.statusLive : ""}`}>
          {confirmed ? (
            <svg aria-hidden="true" viewBox="0 0 16 16">
              <circle cx="8" cy="8" r="6.5" fill="currentColor" />
              <path d="M5 8.2l2 2 4-4.3" fill="none" stroke="white" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          ) : (
            <svg aria-hidden="true" viewBox="0 0 16 16">
              <circle cx="8" cy="8" r="5.5" fill="none" stroke="currentColor" strokeWidth="1.6" />
            </svg>
          )}
          {t(view.pill === "confirmed" ? "contactConfirmedPill" : view.pill === "sent" ? "contactSentPill" : "contactUnconfirmedPill")}
        </span>
        {/* The addresses are text someone typed: only ever rendered as text. */}
        <p className={styles.body}>{t(bodyKey, bodyValues)}</p>
        {view.untilThen && <p className={uiStyles.hint}>{t(view.untilThen[0], view.untilThen[1])}</p>}
        {view.ownerOnly && <p className={uiStyles.hint}>{t("contactOwnerOnly")}</p>}
        {(view.action || view.cancel) && (
          <p className={styles.row}>
            {view.action && (
              <button
                className={view.action === "contactSendAgain" ? uiStyles.textButton : `${uiStyles.button} ${uiStyles.primary}`}
                type="button"
                aria-disabled={busy !== null || undefined}
                onClick={() => act("send")}
              >
                {busy === "send" && <span className={uiStyles.spinner} aria-hidden="true" />}
                {t(view.action)}
              </button>
            )}
            {view.cancel && (
              <button className={uiStyles.textButton} type="button" aria-disabled={busy !== null || undefined} onClick={() => act("cancel")}>
                {busy === "cancel" && <span className={uiStyles.spinner} aria-hidden="true" />}
                {t("contactCancel")}
              </button>
            )}
          </p>
        )}
        {problem && <Banner tone={problem === "contactFailed" ? "error" : "note"}>{t(problem)}</Banner>}
      </div>
    );
  }
  return (
    <>
      {content}
      {live}
    </>
  );
}
