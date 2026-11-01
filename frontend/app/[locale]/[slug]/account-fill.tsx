"use client";

import { type useTranslations } from "next-intl";
import { type Ref, useEffect, useRef } from "react";

import type { AccountOut } from "@/api-client";

import { AccountSignIn } from "../_ui/account-sign-in";
import styles from "../_ui/ui.module.css";
import { CloseIcon, OkCircleIcon, PersonIcon } from "./icons";

type T = ReturnType<typeof useTranslations>;

/**
 * ZIF-146: the checkout's offer to fill in the details from a ziftbook account. It takes nothing the person typed:
 * the offer reads the same whatever is in the form, and nothing looks up a typed email. Its click reads
 * `GET /api/account` (security B3.4: never on load), or opens the sign-in dialog when no account is signed in.
 */
export function AccountOffer({ t, busy, error, onOpen }: { t: T; busy: boolean; error: string | null; onOpen(): void }) {
  return (
    <div className={styles.acctOffer}>
      <PersonIcon />
      <span>{t("signInOffer")}</span>
      <button className={styles.textButton} type="button" aria-disabled={busy || undefined} onClick={onOpen}>
        {t("signInOfferAction")}
      </button>
      {error && (
        <span className={styles.acctOfferError} role="alert">
          {error}
        </span>
      )}
    </div>
  );
}

/** The sign-in dialog: the service dialog's shape, a card on desktop and full screen on a phone. It closes on the
 * same step, with nothing typed so far lost (the message and the marketing tick stay). */
export function SignInDialog({
  open,
  t,
  turnstileSiteKey,
  onClose,
  onSignedIn,
}: {
  open: boolean;
  t: T;
  turnstileSiteKey: string | null;
  onClose(): void;
  onSignedIn(account: AccountOut): void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  return (
    <dialog
      ref={ref}
      className={styles.svcDialog}
      aria-labelledby="acct-dialog-h"
      onClose={() => {
        if (open) onClose();
      }}
      // The body carries the padding, so a click whose target is the <dialog> itself can only land on the backdrop.
      onClick={(event) => {
        if (event.target === ref.current) ref.current?.close();
      }}
    >
      {open && (
        <>
          <button className={`${styles.button} ${styles.secondary} ${styles.small} ${styles.svcAdd} ${styles.svcDialogClose}`} type="button" aria-label={t("close")} onClick={() => ref.current?.close()}>
            <CloseIcon />
          </button>
          <div className={styles.acctDialogBody}>
            <AccountSignIn
              turnstileSiteKey={turnstileSiteKey}
              forgotNewTab
              onSignedIn={onSignedIn}
              intro={
                <>
                  <h2 id="acct-dialog-h" className={styles.acctDialogTitle}>
                    {t("signInTitle")}
                  </h2>
                  <p className={styles.hint}>{t("signInLede")}</p>
                </>
              }
              aside={<p className={styles.aside}>{t("signInGuest")}</p>}
            />
          </div>
        </>
      )}
    </dialog>
  );
}

/** After filling: who is signed in, and that the details are filled in and still editable. */
export function SignedInNote({ account, t, noteRef }: { account: AccountOut; t: T; noteRef: Ref<HTMLDivElement> }) {
  return (
    <div className={styles.acctNote}>
      <div className={styles.statusNote} ref={noteRef} tabIndex={-1} role="status">
        <OkCircleIcon />
        <p>
          <b>{t("signedInAs", { name: account.name || account.email })}</b> {t("filledIn")}
        </p>
      </div>
    </div>
  );
}
