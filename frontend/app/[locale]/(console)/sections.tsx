"use client";

import { useLocale, useTranslations } from "next-intl";

import { Link } from "@/i18n/navigation";
import { todayLabel } from "@/lib/console";

import { useConsole } from "../_ui/console";
import styles from "../_ui/console.module.css";
import { Heading } from "../_ui/parts";
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
          <strong>{t("workerEmptyTitle")}</strong>
          <span>{t("workerEmptyBody")}</span>
        </div>
        <Link className={`${uiStyles.button} ${uiStyles.secondary}`} href="/my-hours">
          {t("checkHours")}
        </Link>
      </>
    );
  }

  return (
    <>
      <Heading focus>{nav("today")}</Heading>
      <p className={uiStyles.lede}>{label}</p>
      <div className={uiStyles.empty}>
        <strong>{t("ownerEmptyTitle")}</strong>
        <span>{t("ownerEmptyBody")}</span>
      </div>
      <ol className={styles.steps} aria-label={t("steps")}>
        <li>
          <span className={styles.stepMark} aria-hidden="true">
            1
          </span>
          <Link href="/opening-hours">{t("step1")}</Link>
        </li>
        <li>
          <span className={styles.stepMark} aria-hidden="true">
            2
          </span>
          <Link href="/services">{t("step2")}</Link>
        </li>
        <li>
          <span className={styles.stepMark} aria-hidden="true">
            3
          </span>
          <Link href={`/team/${session.member_id}`}>{t("step3")}</Link>
        </li>
        <li>
          <span className={styles.stepMark} aria-hidden="true">
            4
          </span>
          <Link href="/team">{t("step4")}</Link>
        </li>
      </ol>
      <p className={uiStyles.hint}>{t("stepsHint")}</p>
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
        <strong>{t("title")}</strong>
        <p>{t("body")}</p>
        {session.role === "owner" ? (
          <Link className={`${uiStyles.button} ${uiStyles.secondary}`} href={`/team/${session.member_id}`} style={{ justifySelf: "start" }}>
            {t("seeHours")}
          </Link>
        ) : (
          <Link className={`${uiStyles.button} ${uiStyles.secondary}`} href="/my-hours" style={{ justifySelf: "start" }}>
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
        <strong>{t("title")}</strong>
        <p>{t("body")}</p>
      </div>
    </>
  );
}

export function Settings() {
  const { settings } = useConsole();
  const t = useTranslations("Console.settings");
  const nav = useTranslations("Console.nav");
  return (
    <>
      <Heading focus>{nav("settings")}</Heading>
      <div className={styles.placeholder}>
        <strong>{t("title")}</strong>
        <p>{t("intro")}</p>
        <ul className={styles.settingsList}>
          <li>{t.rich("timezone", { b: (chunks) => <b>{chunks}</b> })}</li>
          <li>{t.rich("staffHours", { b: (chunks) => <b>{chunks}</b> })}</li>
          <li>{t.rich("buffer", { b: (chunks) => <b>{chunks}</b> })}</li>
          <li>{t.rich("horizon", { b: (chunks) => <b>{chunks}</b>, days: settings.booking_horizon_days })}</li>
          <li>{t.rich("slotStep", { b: (chunks) => <b>{chunks}</b> })}</li>
        </ul>
        <p>{t("openingHoursNote")}</p>
      </div>
    </>
  );
}
