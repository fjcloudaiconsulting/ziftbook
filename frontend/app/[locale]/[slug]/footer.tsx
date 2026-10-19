"use client";

import { useTranslations } from "next-intl";

import { Link } from "@/i18n/navigation";

import styles from "../_ui/ui.module.css";

/** A link that opens a new tab, announced as such to screen readers. */
export function NewTab({ href, children }: { href: string; children: React.ReactNode }) {
  const t = useTranslations("BookingPage");
  return (
    <Link href={href} target="_blank">
      {children}
      <span className={styles.srOnly}> {t("newTab")}</span>
    </Link>
  );
}

/** The booking pages' minimal footer (ZIF-147). Links open a new tab: leaving would lose a booking
 * half filled in. */
export function BookingFooter() {
  const t = useTranslations("BookingPage");
  return (
    <footer className={styles.minimalFooter}>
      <span>
        {t("footerBy")} <b>ziftbook</b>
      </span>
      <NewTab href="/terms">{t("footerTerms")}</NewTab>
      <NewTab href="/privacy">{t("footerPrivacy")}</NewTab>
    </footer>
  );
}
