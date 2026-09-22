"use client";

import { useLocale, useTranslations } from "next-intl";

import { Link, usePathname } from "@/i18n/navigation";
import { routing } from "@/i18n/routing";

import styles from "./ui.module.css";

// Each language names itself, as on the landing page.
const NAMES: Record<(typeof routing.locales)[number], string> = { en: "English", nl: "Nederlands", pt: "Português" };

/** The language list, shared by the signed-out header's popover and the console's account popover. */
export function LanguageLinks() {
  const locale = useLocale();
  const pathname = usePathname();
  return (
    <ul>
      {routing.locales.map((id) => (
        <li key={id}>
          {/* The fragment of an emailed link was already taken out of the address bar. */}
          <Link href={pathname} locale={id} hrefLang={id} lang={id} aria-current={id === locale ? "page" : undefined}>
            {NAMES[id]}
          </Link>
        </li>
      ))}
    </ul>
  );
}

export function Header() {
  const t = useTranslations("Header");

  return (
    <header className={styles.top}>
      <Link className={styles.wordmark} href="/">
        ziftbook
      </Link>
      <button
        className={styles.languageButton}
        type="button"
        popoverTarget="languages"
        aria-label={`${t("language")}: ${t("languageName")}`}
      >
        <svg aria-hidden="true" viewBox="0 0 20 20" width="18" height="18">
          <circle cx="10" cy="10" r="7.25" fill="none" stroke="currentColor" strokeWidth="1.5" />
          <path
            d="M2.75 10h14.5M10 2.75c2 2.1 3 4.5 3 7.25s-1 5.15-3 7.25c-2-2.1-3-4.5-3-7.25s1-5.15 3-7.25z"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
          />
        </svg>
        <span>{t("languageName")}</span>
        <svg aria-hidden="true" className={styles.chevron} viewBox="0 0 12 12" width="12" height="12">
          <path d="M3 4.5l3 3 3-3" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
      <nav id="languages" className={styles.languageMenu} popover="auto" aria-label={t("language")}>
        <LanguageLinks />
      </nav>
    </header>
  );
}
