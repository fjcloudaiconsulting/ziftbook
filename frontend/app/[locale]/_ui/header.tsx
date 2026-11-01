"use client";

import { useLocale, useTranslations } from "next-intl";
import { type MouseEvent, useEffect, useId, useRef, useState } from "react";

import { accountSetLocale, sessionRead } from "@/api-client";
import { Link, usePathname, useRouter } from "@/i18n/navigation";
import { routing } from "@/i18n/routing";
import { isAdminPath, pickedHere, recordPick, resolveLocale, savePickedLanguage, takeUnsavedNotice } from "@/lib/locale";

import { send } from "./parts";
import styles from "./ui.module.css";

type Locale = (typeof routing.locales)[number];

// Each language names itself, as on the landing page. Untranslated by design: these are endonyms,
// not messages, so they never route through the catalogs.
export const NAMES: Record<Locale, string> = { en: "English", nl: "Nederlands", pt: "Português" };

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

/** ZIF-102: the signed-in person the language button is for, and how to save a pick to their
 * account (true once it holds it). */
export type OwnLanguage = { user: string; save(locale: Locale): Promise<boolean> };

/** The language list, shared by the signed-out header's popover and the console's. With `own`
 * (a signed-in person), a pick is saved to their account before the page changes language. */
export function LanguageLinks({ own }: { own?: OwnLanguage }) {
  const locale = useLocale();
  const pathname = usePathname();
  const router = useRouter();
  const [busy, setBusy] = useState(false);

  async function pick(event: MouseEvent, id: Locale) {
    if (!own) return; // signed out: the link only changes this view, as before
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    const saved = await own.save(id);
    setBusy(false);
    // Not saved: it still applies here, for now (resolveLocale's `picked`), and the menu says so.
    recordPick(own.user, id, saved);
    if (id !== locale) router.replace(pathname, { locale: id });
  }

  return (
    <ul aria-busy={busy || undefined}>
      {routing.locales.map((id) => (
        <li key={id}>
          {/* The fragment of an emailed link was already taken out of the address bar. */}
          <Link
            href={pathname}
            locale={id}
            hrefLang={id}
            lang={id}
            aria-current={id === locale ? "page" : undefined}
            onClick={own ? (event) => void pick(event, id) : undefined}
          >
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
 * anyone not reading the flag. With `own` (ZIF-102), the menu is the person's own language: a
 * pick is saved to their account, and the menu says so.
 */
export function LanguageSwitcher({ own }: { own?: OwnLanguage }) {
  const t = useTranslations("Header");
  const locale = useLocale() as Locale;
  const id = useId();
  const menu = useRef<HTMLElement>(null);
  const user = own?.user;
  const unsaved = user !== undefined && pickedHere(user) === locale;

  // The page a pick that could not be saved led to: open the menu once, where its note says so.
  useEffect(() => {
    if (user !== undefined && takeUnsavedNotice(user, locale)) menu.current?.showPopover?.();
  }, [user, locale]);

  return (
    <>
      <button className={styles.languageButton} type="button" popoverTarget={id} aria-label={`${t("language")}: ${t("languageName")}`}>
        <Flag locale={locale} />
      </button>
      <nav ref={menu} id={id} className={styles.languageMenu} popover="auto" aria-label={own ? t("yourLanguage") : t("language")}>
        {own && <p className={styles.languageHead}>{t("yourLanguage")}</p>}
        <LanguageLinks own={own} />
        {own && (
          <p className={styles.languageFoot} role="status">
            {unsaved ? t("languageNotSaved") : t("languageSaved")}
          </p>
        )}
      </nav>
    </>
  );
}

/**
 * ZIF-102, ruling 4: on a public page, the booking page included, a signed-in viewer's own language
 * wins over the link (CONTRIBUTING.md, "Which language a person sees"); a guest keeps the page's.
 * Reads GET /api/session once per page, moves the page when needed, and answers the save for the
 * language button (undefined for a guest). `hold`: the page must not move now (the checkout
 * resuming a booking from its address, where a move would remount it mid-check). Never on the
 * platform admin's pages (isAdminPath).
 */
export function useOwnLanguage({ hold = false }: { hold?: boolean } = {}): OwnLanguage | undefined {
  const locale = useLocale();
  const pathname = usePathname();
  const router = useRouter();
  const admin = isAdminPath(pathname);
  // The person the page was read for: a pick saves to them only (savePickedLanguage).
  const [viewer, setViewer] = useState<{ user_id: string; tenant_id: string } | null>(null);

  useEffect(() => {
    if (admin) return;
    let cancelled = false;
    void send(sessionRead()).then((outcome) => {
      if (cancelled || outcome.status !== 200 || !outcome.data) return;
      setViewer({ user_id: outcome.data.user_id, tenant_id: outcome.data.tenant_id });
      if (hold) return;
      const own = resolveLocale({
        own: outcome.data.locale,
        signedIn: true,
        surface: "public",
        browser: [],
        business: null,
        url: locale as Locale,
        picked: pickedHere(outcome.data.user_id),
      });
      if (own !== locale) router.replace(`${pathname}${window.location.search}`, { locale: own });
    });
    return () => {
      cancelled = true;
    };
    // Once per page: a pick made here afterwards is the person's (and saved, or `picked`).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (admin || !viewer) return undefined;
  return {
    user: viewer.user_id,
    async save(picked) {
      const result = await savePickedLanguage(viewer, () => send(sessionRead()), () => send(accountSetLocale({ body: { locale: picked } })));
      // Someone else signed in in another tab: start over as them, as the console does.
      if (result === "mismatch") window.location.reload();
      // Signed out meanwhile: the button changes only this view again.
      if (result === "signedOut") setViewer(null);
      return result === "saved";
    },
  };
}

/** The language button of a public page, for a signed-in viewer their own language (useOwnLanguage). */
export function OwnLanguageSwitcher({ hold = false }: { hold?: boolean }) {
  return <LanguageSwitcher own={useOwnLanguage({ hold })} />;
}

export function Header() {
  return (
    <header className={styles.top}>
      <Link className={styles.wordmark} href="/">
        ziftbook
      </Link>
      <OwnLanguageSwitcher />
    </header>
  );
}
