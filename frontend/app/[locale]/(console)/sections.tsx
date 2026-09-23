"use client";

import { useLocale, useTranslations } from "next-intl";

import { Link } from "@/i18n/navigation";
import { todayLabel } from "@/lib/console";

import { useConsole } from "../_ui/console";
import styles from "../_ui/console.module.css";
import { Heading, Mark } from "../_ui/parts";
import uiStyles from "../_ui/ui.module.css";

export function Today() {
  const { session, settings } = useConsole();
  const t = useTranslations("Console.today");
  const nav = useTranslations("Console.nav");
  const locale = useLocale();
  const label = todayLabel(new Date(), settings.timezone, locale);

  if (session.role === "worker") {
    return (
      <>
        <Heading focus>{nav("today")}</Heading>
        <p className={uiStyles.lede}>{label}</p>
        <div className={uiStyles.empty}>
          <Mark icon="calendar" />
          <strong>{t("workerEmptyTitle")}</strong>
          <span>{t("workerEmptyBody")}</span>
          <Link className={`${uiStyles.button} ${uiStyles.secondary}`} href="/my-hours">
            {t("checkHours")}
          </Link>
        </div>
      </>
    );
  }

  return (
    <>
      <Heading focus>{nav("today")}</Heading>
      <p className={uiStyles.lede}>{label}</p>
      <div className={uiStyles.empty}>
        <Mark icon="calendar" />
        <strong>{t("ownerEmptyTitle")}</strong>
        <span>{t("ownerEmptyBody")}</span>
      </div>
      <ol className={styles.steps} aria-label={t("steps")}>
        <li>
          <span className={styles.stepMark} aria-hidden="true">
            {1}
          </span>
          <Link className={uiStyles.textButton} href="/opening-hours">
            {t("step1")}
          </Link>
        </li>
        <li>
          <span className={styles.stepMark} aria-hidden="true">
            {2}
          </span>
          <Link className={uiStyles.textButton} href="/services">
            {t("step2")}
          </Link>
        </li>
        <li>
          <span className={styles.stepMark} aria-hidden="true">
            {3}
          </span>
          <Link className={uiStyles.textButton} href={`/team/${session.member_id}`}>
            {t("step3")}
          </Link>
        </li>
        <li>
          <span className={styles.stepMark} aria-hidden="true">
            {4}
          </span>
          <Link className={uiStyles.textButton} href="/team">
            {t("step4")}
          </Link>
        </li>
      </ol>
      <p className={styles.stepsHint}>{t("stepsHint")}</p>
    </>
  );
}

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

export function Settings() {
  const t = useTranslations("Console.settings");
  const nav = useTranslations("Console.nav");
  return (
    <>
      <Heading focus>{nav("settings")}</Heading>
      <div className={styles.placeholder}>
        <p>{t("comingSoon")}</p>
      </div>
    </>
  );
}
