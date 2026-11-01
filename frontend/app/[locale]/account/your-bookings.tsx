"use client";

import { useLocale, useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import { type AccountOut, type HistoryBooking, platformAccountHistory, platformAccountRead } from "@/api-client";
import { Link, useRouter } from "@/i18n/navigation";
import { bookAgainSlug, historyOutcome, historySections, historyServiceName, type Pill, statusPill } from "@/lib/client-account";
import { dateLocale } from "@/lib/console";

import { shownAccount } from "../_ui/account-button";
import { AccountSignIn } from "../_ui/account-sign-in";
import { Banner, NoScript, problem, send, Submit } from "../_ui/parts";
import styles from "../_ui/ui.module.css";

const PAGE = 50;
const PILL_CLASS: Record<Pill["tone"], string> = { ok: styles.pillOk, warn: styles.pillWarn, muted: styles.pillMuted };

type Phase = "loading" | "list" | "reauth" | "error";

/** Every booking this account placed, upcoming then past. Rows open nothing: the history carries no booking id and
 * no manage link (security N9; the confirmation email keeps the link). Past rows offer Book again. */
export function YourBookings({ turnstileSiteKey }: { turnstileSiteKey: string | null }) {
  const t = useTranslations("ClientHome");
  const form = useTranslations("Form");
  const locale = useLocale();
  const router = useRouter();
  const [account, setAccount] = useState<AccountOut | null>(null);
  const [phase, setPhase] = useState<Phase>("loading");
  const [bookings, setBookings] = useState<HistoryBooking[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "error" | "note"; text: string } | null>(null);

  /** One page of the history; `cursor` null starts over. */
  async function loadPage(cursor: string | null) {
    setBusy(true);
    const answer = await send(platformAccountHistory({ query: { limit: PAGE, ...(cursor ? { cursor } : {}) } }));
    setBusy(false);
    const meaning = historyOutcome(answer.status, answer.code);
    if (meaning === "ok" && answer.data) {
      const page = answer.data;
      setBookings((shown) => (cursor ? [...shown, ...page.bookings] : page.bookings));
      setNext(page.next);
      setMessage(null);
      setPhase("list");
    } else if (meaning === "signIn") router.replace("/account/sign-in");
    else if (meaning === "reauth") setPhase("reauth");
    else {
      setMessage({ tone: meaning === "tooMany" ? "note" : "error", text: meaning === "tooMany" ? t("tooMany") : form(problem(answer)) });
      if (!cursor) setPhase("error");
    }
  }

  /** Who is signed in, then the first page: a 401 on either is the sign-in page. */
  async function start(alive: () => boolean = () => true) {
    const me = await send(platformAccountRead());
    if (!alive()) return;
    if (me.status === 401) return router.replace("/account/sign-in");
    if (me.status !== 200 || !me.data) {
      setMessage({ tone: "error", text: form(problem(me)) });
      return setPhase("error");
    }
    setAccount(me.data);
    shownAccount.show(me.data);
    await loadPage(null);
  }

  // A tick after mounting (never a state update in the effect's own body).
  useEffect(() => {
    let cancelled = false;
    const timer = setTimeout(() => void start(() => !cancelled));
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
    // Mount only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const head = (
    <>
      <h1 className={styles.heading}>{t("title")}</h1>
      <p className={styles.lede}>{t("lede")}</p>
      <NoScript>{form("needsJavaScript")}</NoScript>
    </>
  );

  if (phase === "loading") return head;
  if (phase === "reauth" && account) {
    return (
      <>
        {head}
        <AccountSignIn
          turnstileSiteKey={turnstileSiteKey}
          initialEmail={account.email}
          intro={<Banner tone="info">{t("reauth")}</Banner>}
          onSignedIn={(signedIn) => {
            setAccount(signedIn);
            shownAccount.show(signedIn);
            void loadPage(null);
          }}
        />
      </>
    );
  }
  if (phase === "error") {
    return (
      <>
        {head}
        {message && <Banner tone={message.tone}>{message.text}</Banner>}
        <button className={`${styles.button} ${styles.secondary}`} type="button" onClick={() => void start()}>
          {form("tryAgain")}
        </button>
      </>
    );
  }

  const { upcoming, past } = historySections(bookings, new Date());
  const day = new Intl.DateTimeFormat(dateLocale(locale), { weekday: "short", day: "numeric", month: "short" });
  const time = new Intl.DateTimeFormat(dateLocale(locale), { hour: "2-digit", minute: "2-digit" });
  const row = (b: HistoryBooking, ahead: boolean, i: number) => {
    const service = historyServiceName(b.service_name, locale);
    const start = new Date(b.starts_at);
    const when = ahead ? `${day.format(start)}, ${time.format(start)}` : day.format(start);
    const pill = statusPill(b.status, ahead);
    const slug = bookAgainSlug(b, ahead);
    return (
      <li key={`${b.starts_at}-${i}`} className={styles.rowStatic}>
        <span className={styles.rowMain}>
          <span className={styles.rowTitle}>{b.business.name}</span>
          <span className={styles.rowMeta}>
            <span lang={service.lang ?? undefined}>{service.text}</span> · {when}
          </span>
        </span>
        {(pill || slug) && (
          <span className={styles.rowEnd}>
            {pill && <span className={`${styles.pill} ${PILL_CLASS[pill.tone]}`}>{t(pill.label)}</span>}
            {slug && (
              <Link className={`${styles.button} ${styles.secondary} ${styles.small}`} href={`/${slug}`}>
                {t("bookAgain")}
              </Link>
            )}
          </span>
        )}
      </li>
    );
  };

  return (
    <>
      {head}
      {bookings.length === 0 ? (
        <div className={styles.empty}>
          <strong>{t("emptyTitle")}</strong>
          <span>{t("emptyBody")}</span>
        </div>
      ) : (
        <>
          {upcoming.length > 0 && (
            <>
              <h2 className={styles.subhead}>{t("upcoming")}</h2>
              <ul className={styles.list}>{upcoming.map((b, i) => row(b, true, i))}</ul>
            </>
          )}
          {past.length > 0 && (
            <>
              <h2 className={styles.subhead}>{t("past")}</h2>
              <ul className={styles.list}>{past.map((b, i) => row(b, false, i))}</ul>
            </>
          )}
        </>
      )}
      {message && <Banner tone={message.tone}>{message.text}</Banner>}
      {next && (
        <form
          className={styles.form}
          method="post"
          onSubmit={(event) => {
            event.preventDefault();
            if (!busy) void loadPage(next);
          }}
        >
          <Submit busy={busy} busyLabel={t("loadingMore")} secondary>
            {t("more")}
          </Submit>
        </form>
      )}
    </>
  );
}
