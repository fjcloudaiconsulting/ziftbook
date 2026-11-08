"use client";

import { useLocale, useTranslations } from "next-intl";
import { type FormEvent, useEffect, useId, useState } from "react";

import { Link, useRouter } from "@/i18n/navigation";

import { adminListBusinesses, type BusinessPage } from "@/api-client";
import { needsSignIn, pageCount, shortDate } from "@/lib/admin";

import { Banner, Chevron, Heading, problem, Screen, send } from "../../_ui/parts";
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
  const [answer, setAnswer] = useState<BusinessPage | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  useEffect(() => {
    let current = true;
    send(adminListBusinesses({ query })).then((outcome) => {
      if (!current) return; // a later search has taken over
      if (needsSignIn(outcome.status)) router.replace("/admin/sign-in");
      else if (outcome.data) {
        setAnswer(outcome.data);
        setFailure(null);
      } else setFailure(form(problem(outcome)));
    });
    return () => {
      current = false;
    };
  }, [query, router, form]);

  function search(event: FormEvent) {
    event.preventDefault();
    setQuery({ q: typed.trim(), page: 1 });
  }

  const region = new Intl.DisplayNames([locale], { type: "region" });
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
                          t("createdOn", { date: shortDate(business.created_at, locale) }),
                          t("memberCount", { count: business.members }),
                        ].join(" · ")}
                      </span>
                    </span>
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
                disabled={query.page <= 1}
                onClick={() => setQuery({ ...query, page: query.page - 1 })}
              >
                {t("previous")}
              </button>
              <span>{t("pageOf", { page: query.page, pages })}</span>
              <button
                className={`${styles.button} ${styles.secondary} ${styles.small}`}
                type="button"
                disabled={query.page >= pages}
                onClick={() => setQuery({ ...query, page: query.page + 1 })}
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
