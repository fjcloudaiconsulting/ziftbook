"use client";

import { useTranslations } from "next-intl";

import { Link } from "@/i18n/navigation";

import styles from "../_ui/ui.module.css";

/** The booking pages' minimal footer (ZIF-147). Links open a new tab: leaving would lose a booking
 * half filled in. */
export function BookingFooter() {
  const t = useTranslations("BookingPage");
  return (
    <footer className={styles.minimalFooter}>
      <span>
        {t("footerBy")} <b>ziftbook</b>
      </span>
      <Link href="/terms" target="_blank">
        {t("footerTerms")}
        <span className={styles.srOnly}> {t("newTab")}</span>
      </Link>
      <Link href="/privacy" target="_blank">
        {t("footerPrivacy")}
        <span className={styles.srOnly}> {t("newTab")}</span>
      </Link>
    </footer>
  );
}
