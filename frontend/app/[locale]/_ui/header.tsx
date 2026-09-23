"use client";

import { useLocale, useTranslations } from "next-intl";
import { useId } from "react";

import { Link, usePathname } from "@/i18n/navigation";
import { routing } from "@/i18n/routing";

import styles from "./ui.module.css";

type Locale = (typeof routing.locales)[number];

// Each language names itself, as on the landing page. Untranslated by design: these are endonyms,
// not messages, so they never route through the catalogs.
const NAMES: Record<Locale, string> = { en: "English", nl: "Nederlands", pt: "Português" };

// One small inline flag per language, next to NAMES: adding a language later means adding a
// catalog, a name here, a flag here, and a routing entry, nothing else. Never emoji flags: they
// render as bare letter pairs on Windows.
const FLAGS: Record<Locale, React.ReactNode> = {
  en: (
    <>
      <rect width="20" height="14" fill="#00247d" />
      <path d="M0 0L20 14M20 0L0 14" stroke="#fff" strokeWidth="2.8" />
      <path d="M0 0L20 14M20 0L0 14" stroke="#cf142b" strokeWidth="1.2" />
      <path d="M10 0V14M0 7H20" stroke="#fff" strokeWidth="4.6" />
      <path d="M10 0V14M0 7H20" stroke="#cf142b" strokeWidth="2.6" />
    </>
  ),
  nl: (
    <>
      <rect width="20" height="14" fill="#fff" />
      <rect width="20" height="4.67" fill="#ae1c28" />
      <rect y="9.33" width="20" height="4.67" fill="#21468b" />
    </>
  ),
  pt: (
    <>
      <rect width="20" height="14" fill="#ff0000" />
      <rect width="8" height="14" fill="#046a38" />
      <circle cx="8" cy="7" r="2.6" fill="#ffcc00" stroke="#046a38" strokeWidth="0.4" />
    </>
  ),
};

function Flag({ locale }: { locale: Locale }) {
  const clipId = useId();
  return (
    <svg aria-hidden="true" viewBox="0 0 20 14" width="20" height="14" className={styles.flag}>
      <clipPath id={clipId}>
        <rect width="20" height="14" rx="2" />
      </clipPath>
      <g clipPath={`url(#${clipId})`}>{FLAGS[locale]}</g>
    </svg>
  );
}

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
            <span className={styles.languageRow}>
              <Flag locale={id} />
              {NAMES[id]}
            </span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

/**
 * The language button and its popover: one component for the signed-out header and the console
 * top bar, so there is only ever one switcher to keep in sync. Closed, it shows only the current
 * language's flag; its accessible name (e.g. "Language: English") still names the language, for
 * anyone not reading the flag.
 */
export function LanguageSwitcher() {
  const t = useTranslations("Header");
  const locale = useLocale() as Locale;
  const id = useId();

  return (
    <>
      <button className={styles.languageButton} type="button" popoverTarget={id} aria-label={`${t("language")}: ${t("languageName")}`}>
        <Flag locale={locale} />
      </button>
      <nav id={id} className={styles.languageMenu} popover="auto" aria-label={t("language")}>
        <LanguageLinks />
      </nav>
    </>
  );
}

export function Header() {
  return (
    <header className={styles.top}>
      <Link className={styles.wordmark} href="/">
        ziftbook
      </Link>
      <LanguageSwitcher />
    </header>
  );
}
