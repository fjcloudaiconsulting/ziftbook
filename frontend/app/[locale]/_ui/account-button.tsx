"use client";

import { useTranslations } from "next-intl";
import { useId, useState, useSyncExternalStore } from "react";

import { type AccountOut, platformAccountSignOut } from "@/api-client";
import { Link, useRouter } from "@/i18n/navigation";
import { accountStore } from "@/lib/account-store";

import { JSON_WRITE, type Outcome, problem, send } from "./parts";
import styles from "./ui.module.css";

/** The one store this page's account button reads (lib/account-store.ts). */
export const shownAccount = accountStore<AccountOut>();

/** Ends this browser's account session (DELETE /api/account/session, which ends its console session too); on success
 * the button goes and every page listening empties what the account filled in. */
export async function signOutAccount(): Promise<Outcome<unknown>> {
  const answer = await send(platformAccountSignOut({ ...JSON_WRITE }));
  if (answer.status === 204) shownAccount.signedOut();
  return answer;
}

/**
 * ZIF-146: the account button on the booking and account pages, once this page knows who is signed in (never from a
 * render on the server, never on its own: after the person signed in or filled in). Its menu: who, "Your bookings"
 * and "Sign out". Never on a console page. `signInAfter`: an account page goes to the sign-in page once signed out.
 */
export function AccountButton({ signInAfter = false }: { signInAfter?: boolean }) {
  const t = useTranslations("ClientHome");
  const form = useTranslations("Form");
  const router = useRouter();
  const id = useId();
  const account = useSyncExternalStore(shownAccount.subscribe, shownAccount.get, () => null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!account) return null;

  async function onSignOut() {
    if (busy) return;
    setBusy(true);
    setError(null);
    const answer = await signOutAccount();
    setBusy(false);
    if (answer.status !== 204) setError(form(problem(answer)));
    else if (signInAfter) router.replace("/account/sign-in");
  }

  return (
    <>
      <button className={styles.accountButton} type="button" popoverTarget={id} aria-label={t("menu")}>
        <svg aria-hidden="true" viewBox="0 0 24 24" width="18" height="18">
          <circle cx="12" cy="9" r="3.5" fill="none" stroke="currentColor" strokeWidth="1.8" />
          <path d="M5 19.5c1.2-3.3 3.8-5 7-5s5.8 1.7 7 5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
        </svg>
      </button>
      <div id={id} className={styles.accountMenu} popover="auto" aria-label={t("menu")}>
        <p className={styles.accountMenuName}>{account.name || account.email}</p>
        {account.name && <p className={styles.accountMenuMeta}>{account.email}</p>}
        <div className={styles.accountMenuDivider} role="presentation" />
        <div className={styles.stack}>
          <Link className={`${styles.button} ${styles.secondary}`} href="/account">
            {t("title")}
          </Link>
          <button className={styles.textButton} type="button" aria-disabled={busy || undefined} onClick={() => void onSignOut()}>
            {t("signOut")}
          </button>
          {error && (
            <p className={styles.hint} role="alert">
              {error}
            </p>
          )}
        </div>
      </div>
    </>
  );
}
