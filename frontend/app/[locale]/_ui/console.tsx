"use client";

import { useTranslations } from "next-intl";
import { createContext, type ReactNode, useContext, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import {
  type BusinessSettingsOutput,
  type SessionOut,
  sessionRead,
  sessionSignOut,
  sessionSignOutEverywhere,
  settingsRead,
} from "@/api-client";
import { Link, usePathname, useRouter } from "@/i18n/navigation";
import { allowed, guardedWrite, navFor, type Role, sectionOf, type Section, writeOutcome } from "@/lib/console";

import { LanguageSwitcher } from "./header";
import { Banner, NoScript, type Outcome, problem, send } from "./parts";
import styles from "./console.module.css";
import uiStyles from "./ui.module.css";

// The API only accepts JSON writes (its CSRF defence), and the client drops Content-Type from a
// request with no body: these DELETEs send an empty JSON object.
export const JSON_WRITE = { body: {} as never };

type ConsoleContextValue = {
  session: SessionOut;
  settings: BusinessSettingsOutput;
  /** Every write goes through this. `request` is a thunk: a generated SDK call fires its request the
   * instant it's invoked, so an already-started promise here would race the session re-read this
   * runs first for a write (`guardedWrite`). It rereads the session first, so a sign-in as someone
   * else in another tab never lets this tab's write land in the wrong business. */
  call<T>(request: () => Promise<{ data?: T; error?: unknown; response?: Response }>, options?: { write?: boolean }): Promise<Outcome<T>>;
  updateSession(patch: Partial<SessionOut>): void;
  /** Where a page's own bottom action bar (the week editor's savebar) renders on phone, so it
   * stacks directly on the tab bar as one element with no gap between them - never a sticky offset
   * computed to line up with a separately-stickied tab bar, which a page nested many levels deep
   * has no reliable room to reach (`FooterSlot`/`FooterPortal`, below). `null` until mounted. */
  footerSlot: HTMLDivElement | null;
};

const ConsoleContext = createContext<ConsoleContextValue | null>(null);

export function useConsole(): ConsoleContextValue {
  const value = useContext(ConsoleContext);
  if (!value) throw new Error("useConsole must be used inside Shell");
  return value;
}

/** Portals a page's phone-only footer action bar (the week editor's savebar) into the Shell's own
 * bottom bar, right above the tab bar: one sticky container, one set of edges, so no gap can open
 * between the two. Renders nothing until the slot has mounted, and nothing at all on desktop
 * (the slot isn't rendered there - see `.footerSlot` in console.module.css). */
export function FooterPortal({ children }: { children: ReactNode }) {
  const { footerSlot } = useConsole();
  return footerSlot ? createPortal(children, footerSlot) : null;
}

const ICONS: Record<Section, ReactNode> = {
  today: (
    <svg aria-hidden="true" viewBox="0 0 24 24" width="20" height="20">
      <rect x="3.5" y="5" width="17" height="15" rx="2.5" fill="none" stroke="currentColor" strokeWidth="1.7" />
      <path d="M3.5 9.5h17M8 3.5v3M16 3.5v3" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
      <circle cx="8.5" cy="14" r="1.2" fill="currentColor" />
    </svg>
  ),
  calendar: (
    <svg aria-hidden="true" viewBox="0 0 24 24" width="20" height="20">
      <circle cx="12" cy="12" r="8.25" fill="none" stroke="currentColor" strokeWidth="1.7" />
      <path d="M12 7.5v5l3 2" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
    </svg>
  ),
  services: (
    <svg aria-hidden="true" viewBox="0 0 24 24" width="20" height="20">
      <path d="M4.5 7h15M4.5 12h15M4.5 17h9" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
    </svg>
  ),
  team: (
    <svg aria-hidden="true" viewBox="0 0 24 24" width="20" height="20">
      <circle cx="9.5" cy="9" r="3.2" fill="none" stroke="currentColor" strokeWidth="1.7" />
      <path
        d="M3.5 19c.9-2.9 3.2-4.4 6-4.4S14.6 16.1 15.5 19"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
      />
      <path d="M16.5 6.4a3.2 3.2 0 0 1 0 5.2M18 14.9c1.3.7 2.2 2 2.6 4.1" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
    </svg>
  ),
  "my-hours": (
    <svg aria-hidden="true" viewBox="0 0 24 24" width="20" height="20">
      <rect x="3.5" y="5" width="17" height="15" rx="2.5" fill="none" stroke="currentColor" strokeWidth="1.7" />
      <path d="M3.5 9.5h17" fill="none" stroke="currentColor" strokeWidth="1.7" />
      <path d="M8 13.5h8M8 16.5h5" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
    </svg>
  ),
  clients: (
    <svg aria-hidden="true" viewBox="0 0 24 24" width="20" height="20">
      <circle cx="12" cy="9" r="3.4" fill="none" stroke="currentColor" strokeWidth="1.7" />
      <path d="M5 19.3c1.3-3.2 3.9-4.9 7-4.9s5.7 1.7 7 4.9" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
    </svg>
  ),
  "opening-hours": (
    <svg aria-hidden="true" viewBox="0 0 24 24" width="20" height="20">
      <path
        d="M4 9.5h16v9.4a1.4 1.4 0 0 1-1.4 1.4H5.4A1.4 1.4 0 0 1 4 18.9V9.5Z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinejoin="round"
      />
      <path d="M3 9.5 5.2 4.5h13.6L21 9.5" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round" />
      <path d="M9.5 20.3v-5.4h5v5.4" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round" />
    </svg>
  ),
  settings: (
    <svg aria-hidden="true" viewBox="0 0 24 24" width="20" height="20">
      <circle cx="12" cy="12" r="3" fill="none" stroke="currentColor" strokeWidth="1.7" />
      <path
        d="M12 3.5v2.2M12 18.3v2.2M20.5 12h-2.2M5.7 12H3.5M18 6l-1.6 1.6M7.6 16.4 6 18M18 18l-1.6-1.6M7.6 7.6 6 6"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
      />
    </svg>
  ),
};

const HREF: Record<Section, string> = {
  today: "/",
  calendar: "/calendar",
  services: "/services",
  team: "/team",
  "my-hours": "/my-hours",
  clients: "/clients",
  "opening-hours": "/opening-hours",
  settings: "/settings",
};

function NavLink({
  section,
  current,
  className,
  onClick,
  variant = "item",
}: {
  section: Section;
  current: Section | null;
  className: string;
  onClick?(): void;
  /** "tab" ellipsizes its label (the phone tab bar, where space is tight); "item" (sidebar, More
   * sheet) renders it plainly, at the same size as every other row. */
  variant?: "item" | "tab";
}) {
  const t = useTranslations("Console.nav");
  const labelKey = section === "my-hours" ? "myHours" : section === "opening-hours" ? "openingHours" : section;
  const label = t(labelKey);
  return (
    <Link href={HREF[section]} className={className} aria-current={current === section ? "page" : undefined} onClick={onClick}>
      {ICONS[section]}
      {variant === "tab" ? <span className={styles.tabLabel}>{label}</span> : label}
    </Link>
  );
}

function AccountPopover({ session, onSignOut }: { session: SessionOut; onSignOut(everywhere: boolean): void }) {
  const t = useTranslations("Console.account");
  const person = useTranslations("Console.person");

  return (
    <div id="account" className={styles.accountPopover} popover="auto" aria-label={t("menu")}>
      <p className={styles.accountName}>{session.display_name || person("nameNotSet")}</p>
      <p className={styles.accountMeta}>
        {session.email} · {t(session.role === "owner" ? "owner" : "worker")}
      </p>
      <div className={styles.popoverDivider} role="presentation" />
      <div className={uiStyles.stack}>
        <button className={`${uiStyles.button} ${uiStyles.secondary}`} type="button" onClick={() => onSignOut(false)}>
          {t("signOut")}
        </button>
        <p className={uiStyles.hint}>{t("everywhereHint")}</p>
        <button className={uiStyles.textButton} type="button" onClick={() => onSignOut(true)}>
          {t("signOutEverywhere")}
        </button>
      </div>
    </div>
  );
}

/** The banner a write shows when `call()`'s session check finds the cookie already signed out
 * (U3). No PR 1 screen writes anything besides sign-out (which has its own, opposite meaning for a
 * 401: already signed out), so this has no caller yet — PR 2's save button is the first. */
export function SignedOutBanner() {
  const t = useTranslations("Console.errors");
  return (
    <Banner tone="error">
      {t.rich("signedOut", {
        link: (chunks) => (
          <a href="/sign-in" target="_blank" rel="noopener">
            {chunks}
          </a>
        ),
      })}
    </Banner>
  );
}

function MoreSheet({ more, current, open, onClose }: { more: Section[]; current: Section | null; open: boolean; onClose(): void }) {
  const nav = useTranslations("Console.nav");
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  return (
    <dialog
      ref={ref}
      className={styles.sheet}
      aria-label={nav("moreSections")}
      onClose={onClose}
      // The padding sits on the inner wrapper, so a click whose target is the <dialog> itself can
      // only land on the backdrop, never the content.
      onClick={(event) => {
        if (event.target === ref.current) ref.current?.close();
      }}
    >
      <div className={styles.sheetInner}>
        <div className={styles.sheetGrab} aria-hidden="true" />
        {more.map((section) => (
          <NavLink key={section} section={section} current={current} className={styles.navItem} onClick={() => ref.current?.close()} />
        ))}
        <button className={styles.navItem} type="button" onClick={() => ref.current?.close()}>
          <svg aria-hidden="true" viewBox="0 0 24 24" width="20" height="20">
            <path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
          </svg>
          <span>{nav("close")}</span>
        </button>
      </div>
    </dialog>
  );
}

export function Shell({ children }: { children: ReactNode }) {
  const form = useTranslations("Form");
  const navT = useTranslations("Console.nav");
  const accountT = useTranslations("Console.account");
  const router = useRouter();
  const pathname = usePathname();
  const [session, setSession] = useState<SessionOut | null>(null);
  const [settings, setSettings] = useState<BusinessSettingsOutput | null>(null);
  const [failure, setFailure] = useState<ReturnType<typeof problem> | null>(null);
  const [signOutFailure, setSignOutFailure] = useState<ReturnType<typeof problem> | null>(null);
  const [moreOpen, setMoreOpen] = useState(false);
  const [footerSlot, setFooterSlot] = useState<HTMLDivElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([send(sessionRead()), send(settingsRead())]).then(([sessionOutcome, settingsOutcome]) => {
      if (cancelled) return;
      if (sessionOutcome.status === 401 || settingsOutcome.status === 401) {
        router.replace("/sign-in");
        return;
      }
      if (sessionOutcome.status === 200 && sessionOutcome.data && settingsOutcome.status === 200 && settingsOutcome.data) {
        setSession(sessionOutcome.data);
        setSettings(settingsOutcome.data);
      } else {
        setFailure(problem(sessionOutcome.status !== 200 ? sessionOutcome : settingsOutcome));
      }
    });
    return () => {
      cancelled = true;
    };
    // Client navigation keeps this layout mounted, so this reads the session once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (session && !allowed(session.role as Role, pathname)) router.replace("/");
  }, [session, pathname, router]);

  async function call<T>(
    request: () => Promise<{ data?: T; error?: unknown; response?: Response }>,
    options?: { write?: boolean },
  ): Promise<Outcome<T>> {
    if (!options?.write) {
      const outcome = await send(request());
      if (outcome.status === 401) router.replace("/sign-in");
      return outcome;
    }
    const result = await guardedWrite<T>(
      () => send(sessionRead()),
      session!,
      () => send(request()),
    );
    // Nothing was sent: the reload throws this tab's state away, so a bare status back to the
    // caller is fine, but every other mapping (including "the identity check itself failed, don't
    // report that as a success") lives in writeOutcome, not here.
    if (result.kind === "mismatch") window.location.reload();
    return writeOutcome(result);
  }

  function updateSession(patch: Partial<SessionOut>) {
    setSession((current) => (current ? { ...current, ...patch } : current));
  }

  async function onSignOut(everywhere: boolean) {
    const outcome = await send(everywhere ? sessionSignOutEverywhere(JSON_WRITE) : sessionSignOut(JSON_WRITE));
    // 401: the session had already ended, which is where signing out leads anyway.
    if (outcome.status === 204 || outcome.status === 401) router.replace("/sign-in");
    else setSignOutFailure(problem(outcome));
  }

  if (!session || !settings) {
    return failure ? (
      <>
        <header className={styles.topbar}>
          <span className={uiStyles.wordmark}>ziftbook</span>
        </header>
        <main id="content" className={styles.content}>
          <Banner tone="error">{form(failure)}</Banner>
        </main>
      </>
    ) : (
      <NoScript>{form("needsJavaScript")}</NoScript>
    );
  }

  if (!allowed(session.role as Role, pathname)) return null;

  const nav = navFor(session.role as Role);
  const current = sectionOf(pathname);

  return (
    <ConsoleContext.Provider value={{ session, settings, call, updateSession, footerSlot }}>
      <div className={styles.shell}>
        <header className={styles.topbar}>
          <span className={styles.brand}>
            <Link className={uiStyles.wordmark} href="/">
              ziftbook
            </Link>
            <span className={styles.bizName}>{session.business_name}</span>
          </span>
          <div className={styles.topbarActions}>
            <LanguageSwitcher />
            <button className={styles.iconButton} type="button" popoverTarget="account" aria-label={accountT("menu")}>
              <svg aria-hidden="true" viewBox="0 0 24 24" width="18" height="18">
                <circle cx="12" cy="9" r="3.5" fill="none" stroke="currentColor" strokeWidth="1.8" />
                <path d="M5 19.5c1.2-3.3 3.8-5 7-5s5.8 1.7 7 5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
              </svg>
            </button>
            <AccountPopover session={session} onSignOut={onSignOut} />
          </div>
        </header>

        {signOutFailure && (
          <div className={styles.content}>
            <Banner tone="error">{form(signOutFailure)}</Banner>
          </div>
        )}

        <div className={styles.body}>
          <nav className={styles.sidebar} aria-label={navT("sections")}>
            {nav.sidebar.map((section) => (
              <NavLink key={section} section={section} current={current} className={styles.navItem} />
            ))}
          </nav>

          <main id="content" className={styles.content}>
            <div className={styles.col}>{children}</div>
          </main>
        </div>

        {/* One sticky container for a page's own phone footer bar (FooterPortal) stacked directly
            on the tab bar: adjacent children of the same box, so there is no gap either could show
            scrolled content through. Desktop has no tab bar and renders nothing here (a page's
            footer bar sticks on its own there, inside .content - see week-editor.tsx). */}
        <div className={styles.bottomBar}>
          <div ref={setFooterSlot} className={styles.footerSlot} />
          <nav className={styles.tabbar} aria-label={navT("sections")}>
            {nav.tabs.map((section) =>
              section === "more" ? (
                <button
                  key="more"
                  type="button"
                  className={styles.tab}
                  aria-haspopup="dialog"
                  aria-expanded={moreOpen}
                  onClick={() => setMoreOpen(true)}
                >
                  <svg aria-hidden="true" viewBox="0 0 24 24" width="20" height="20">
                    <circle cx="5.5" cy="12" r="1.6" fill="currentColor" />
                    <circle cx="12" cy="12" r="1.6" fill="currentColor" />
                    <circle cx="18.5" cy="12" r="1.6" fill="currentColor" />
                  </svg>
                  <span className={styles.tabLabel}>{navT("more")}</span>
                </button>
              ) : (
                <NavLink key={section} section={section} current={current} className={styles.tab} variant="tab" />
              ),
            )}
          </nav>
        </div>

        <MoreSheet more={nav.more} current={current} open={moreOpen} onClose={() => setMoreOpen(false)} />
      </div>
    </ConsoleContext.Provider>
  );
}
