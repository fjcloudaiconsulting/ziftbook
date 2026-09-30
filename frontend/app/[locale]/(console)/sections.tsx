"use client";

import { useTranslations } from "next-intl";

import { Link } from "@/i18n/navigation";

import { useConsole } from "../_ui/console";
import styles from "../_ui/console.module.css";
import { Heading } from "../_ui/parts";
import uiStyles from "../_ui/ui.module.css";

export function Calendar() {
  const { session } = useConsole();
  const t = useTranslations("Console.calendar");
  const nav = useTranslations("Console.nav");
  return (
    <>
      <Heading focus>{nav("calendar")}</Heading>
      <div className={styles.placeholder}>
        <p>{t("comingSoon")}</p>
        {session.role === "owner" ? (
          <Link className={`${uiStyles.button} ${uiStyles.secondary} ${styles.placeholderAction}`} href={`/team/${session.member_id}`}>
            {t("seeHours")}
          </Link>
        ) : (
          <Link className={`${uiStyles.button} ${uiStyles.secondary} ${styles.placeholderAction}`} href="/my-hours">
            {t("seeMyHours")}
          </Link>
        )}
      </div>
    </>
  );
}

export function Clients() {
  const t = useTranslations("Console.clients");
  const nav = useTranslations("Console.nav");
  return (
    <>
      <Heading focus>{nav("clients")}</Heading>
      <div className={styles.placeholder}>
        <p>{t("comingSoon")}</p>
      </div>
    </>
  );
}
