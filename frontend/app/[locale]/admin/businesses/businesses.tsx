"use client";

import { useLocale, useTranslations } from "next-intl";
import { type FormEvent, useEffect, useId, useState } from "react";

import { Link, useRouter } from "@/i18n/navigation";

import { adminListBusinesses, type BusinessPage } from "@/api-client";
import { needsSignIn, pageCount, shortDate } from "@/lib/admin";
import { dateLocale } from "@/lib/console";

import { Banner, Chevron, Heading, type Outcome, problem, Screen, send } from "../../_ui/parts";
import styles from "../../_ui/ui.module.css";
import admin from "../admin.module.css";
import { BackLink } from "../parts";

/** ZIF-73: every business on this ziftbook, searched by name or slug, a page at a time (the API's size). */
export function Businesses() {
  const t = useTranslations("Admin");
  const form = useTranslations("Form");
  const locale = useLocale();
  const router = useRouter();
  const searchId = useId();
  const [typed, setTyped] = useState("");
  const [query, setQuery] = useState({ q: "", page: 1 });
  // The answer with the query it answers: rows and page number always belong together, and a failed
  // fetch shows its error, not the previous page's rows.
  const [shown, setShown] = useState<{ query: typeof query; outcome: Outcome<BusinessPage> } | null>(null);

  useEffect(() => {
    let current = true;
    send(adminListBusinesses({ query })).then((outcome) => {
      if (current) setShown({ query, outcome }); // else a later search has taken over
    });
    return () => {
      current = false;
    };
  }, [query]);

  useEffect(() => {
    if (shown && needsSignIn(shown.outcome.status)) router.replace("/admin/sign-in");
  }, [shown, router]);

  function search(event: FormEvent) {
    event.preventDefault();
    setQuery({ q: typed.trim(), page: 1 });
  }

  const region = new Intl.DisplayNames([locale], { type: "region" });
  const day = new Intl.DateTimeFormat(dateLocale(locale), { day: "numeric", month: "short", year: "numeric" });
  const answer = shown?.outcome.data;
  const page = shown?.query.page ?? 1;
  const failure = shown && !answer && !needsSignIn(shown.outcome.status) ? form(problem(shown.outcome)) : null;
  const pages = answer ? pageCount(answer.total, answer.page_size) : 1;

  return (
    <Screen wide>
      <BackLink href="/admin">{t("homeTitle")}</BackLink>
      <Heading>{t("businessesTitle")}</Heading>
      <form role="search" className={styles.field} onSubmit={search}>
        <label className={styles.label} htmlFor={searchId}>
          {t("searchLabel")}
        </label>
        <div className={admin.searchRow}>
          <div className={styles.input}>
            <input id={searchId} type="search" name="q" maxLength={100} value={typed} onChange={(event) => setTyped(event.target.value)} />
          </div>
          <button className={`${styles.button} ${styles.secondary} ${styles.small}`} type="submit">
            {t("search")}
          </button>
        </div>
      </form>
      {failure && <Banner tone="error">{failure}</Banner>}
      {answer && (
        <>
          <p className={styles.hint} aria-live="polite">
            {t("businessCount", { count: answer.total })}
          </p>
          {answer.businesses.length > 0 && (
            <ul className={styles.list}>
              {answer.businesses.map((business) => (
                <li key={business.id}>
                  <Link className={styles.rowLink} href={`/admin/businesses/${business.id}`}>
                    <span className={styles.rowMain}>
                      <span className={styles.rowTitle}>{business.name}</span>
                      <span className={`${styles.rowMeta} ${admin.mono}`}>{business.slug}</span>
                      <span className={styles.rowMeta}>
                        {[
                          region.of(business.country) ?? business.country,
                          t("createdOn", { date: shortDate(business.created_at, day) }),
                          t("memberCount", { count: business.members }),
                        ].join(" · ")}
                      </span>
                    </span>
                    {business.suspended_at && <span className={`${styles.pill} ${styles.pillWarn}`}>{t("suspended")}</span>}
                    <span className={styles.pill}>{t(`stripe.${business.stripe}`)}</span>
                    <Chevron />
                  </Link>
                </li>
              ))}
            </ul>
          )}
          {pages > 1 && (
            <nav className={admin.pager} aria-label={t("pagesLabel")}>
              <button
                className={`${styles.button} ${styles.secondary} ${styles.small}`}
                type="button"
                disabled={page <= 1}
                onClick={() => setQuery({ q: shown?.query.q ?? "", page: page - 1 })}
              >
                {t("previous")}
              </button>
              <span>{t("pageOf", { page, pages })}</span>
              <button
                className={`${styles.button} ${styles.secondary} ${styles.small}`}
                type="button"
                disabled={page >= pages}
                onClick={() => setQuery({ q: shown?.query.q ?? "", page: page + 1 })}
              >
                {t("next")}
              </button>
            </nav>
          )}
        </>
      )}
    </Screen>
  );
}
