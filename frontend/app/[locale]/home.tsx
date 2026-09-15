"use client";

import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import { type SessionOut, sessionRead, sessionSignOut, sessionSignOutEverywhere } from "@/api-client";
import { useRouter } from "@/i18n/navigation";

import { Banner, send, styles } from "./_ui/parts";

// The API only accepts JSON writes (its CSRF defence), and the client drops Content-Type from a request with
// no body: these DELETEs send an empty JSON object.
const JSON_WRITE = { body: {} as never };

/** A placeholder home until the booking screens exist: the business, the person, and signing out. */
export function Home() {
  const t = useTranslations("Home");
  const form = useTranslations("Form");
  const router = useRouter();
  const [session, setSession] = useState<SessionOut | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    send(sessionRead()).then((outcome) => {
      if (outcome.status === 200 && outcome.data) setSession(outcome.data);
      else if (outcome.status === 401) router.replace("/sign-in");
      else setFailed(true);
    });
  }, [router]);

  async function signOut(everywhere: boolean) {
    const outcome = await send(everywhere ? sessionSignOutEverywhere(JSON_WRITE) : sessionSignOut(JSON_WRITE));
    // 401: the session had already ended, which is where signing out leads anyway.
    if (outcome.status === 204 || outcome.status === 401) router.replace("/sign-in");
    else setFailed(true);
  }

  if (failed) return <Banner tone="error">{form("unreachable")}</Banner>;
  if (!session) return null;

  return (
    <>
      <h1 className={styles.heading}>{session.business_name}</h1>
      <p className={styles.meta}>{t.rich("signedInAs", { email: session.email, chip: (chunks) => <span className={styles.chip}>{chunks}</span> })}</p>
      <span className={styles.role}>{t(session.role === "owner" ? "owner" : "worker")}</span>
      <div className={styles.empty}>
        <strong>{t("emptyTitle")}</strong>
        <span>{t("emptyBody")}</span>
      </div>
      <div className={styles.divider} role="presentation" />
      <div className={styles.stack}>
        <button className={`${styles.button} ${styles.secondary}`} type="button" onClick={() => signOut(false)}>
          {t("signOut")}
        </button>
        <p className={styles.hint}>{t("everywhereHint")}</p>
        <button className={styles.textButton} type="button" onClick={() => signOut(true)}>
          {t("signOutEverywhere")}
        </button>
      </div>
    </>
  );
}
