"use client";

import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import { useRouter } from "@/i18n/navigation";

import { adminRead, adminSignOut, type AdminSession } from "@/api-client";
import { stepFor } from "@/lib/admin";

import { Banner, Heading, JSON_WRITE, Mark, problem, Screen, send } from "../_ui/parts";
import styles from "../_ui/ui.module.css";
import admin from "./admin.module.css";

/** Until ZIF-72's console: who is signed in, and the way out. */
export function AdminHome() {
  const t = useTranslations("Admin");
  const form = useTranslations("Form");
  const router = useRouter();
  const [who, setWho] = useState<AdminSession | null>(null);
  const [signOutFailure, setSignOutFailure] = useState<string | null>(null);

  useEffect(() => {
    send(adminRead()).then((outcome) => {
      // Anything short of a passed second factor goes back through sign-in, which resumes at the right step.
      if (outcome.data && stepFor(outcome.data.second_factor) === "done") setWho(outcome.data);
      else router.replace("/admin/sign-in");
    });
  }, [router]);

  async function leave() {
    const outcome = await send(adminSignOut(JSON_WRITE));
    // 401: the session had already ended, which is where signing out leads anyway.
    if (outcome.status === 204 || outcome.status === 401) router.replace("/admin/sign-in");
    else setSignOutFailure(form(problem(outcome)));
  }

  return (
    <Screen>
      {who && (
        <>
          <Mark icon="shield" />
          <Heading>{t("homeTitle")}</Heading>
          <p className={styles.lede}>{t("homeLede")}</p>
          <div className={admin.account}>
            <Mark icon="person" />
            <div>
              <strong>{who.name ?? who.email}</strong>
              <span>{who.email}</span>
            </div>
          </div>
          {signOutFailure && <Banner tone="error">{signOutFailure}</Banner>}
          <button className={`${styles.button} ${styles.secondary}`} type="button" onClick={leave}>
            {t("signOut")}
          </button>
        </>
      )}
    </Screen>
  );
}
