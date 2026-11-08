"use client";

import { useLocale, useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import { useRouter } from "@/i18n/navigation";

import { adminReadBusiness, type BusinessDetail } from "@/api-client";
import { needsSignIn, settingText, shortDate } from "@/lib/admin";
import { dateLocale } from "@/lib/console";

import { Banner, Heading, type Outcome, problem, Screen, send } from "../../../_ui/parts";
import styles from "../../../_ui/ui.module.css";
import admin from "../../admin.module.css";
import { BackLink } from "../../parts";

/** ZIF-73: one business, read-only: its members and roles, its settings and its latest audit events. No client's
 * data: the API sends none. Opening it is recorded in the platform audit log. */
export function Business({ id }: { id: string }) {
  const t = useTranslations("Admin");
  const form = useTranslations("Form");
  const locale = useLocale();
  const router = useRouter();
  const [answered, setAnswered] = useState<{ id: string; outcome: Outcome<BusinessDetail> } | null>(null);

  // On the id alone: every GET records a business_read, so nothing else (a new router or translator)
  // may send another, and an answer for an id no longer shown is dropped.
  useEffect(() => {
    let current = true;
    send(adminReadBusiness({ path: { tenant_id: id } })).then((outcome) => {
      if (current) setAnswered({ id, outcome });
    });
    return () => {
      current = false;
    };
  }, [id]);

  const outcome = answered?.id === id ? answered.outcome : null; // another id's answer: still loading
  useEffect(() => {
    if (outcome && needsSignIn(outcome.status)) router.replace("/admin/sign-in");
  }, [outcome, router]);

  const business = outcome?.data;
  const failure =
    !outcome || business || needsSignIn(outcome.status)
      ? null
      : // 422: not an id at all, which is no business either.
        outcome.status === 404 || outcome.status === 422
        ? t("businessNotFound")
        : form(problem(outcome));
  const names = new Map(business?.members.map((member) => [member.user_id, member.name ?? t("unnamed")]));
  const day = new Intl.DateTimeFormat(dateLocale(locale), { day: "numeric", month: "short", year: "numeric" });
  const moment = new Intl.DateTimeFormat(dateLocale(locale), { dateStyle: "medium", timeStyle: "short" });

  return (
    <Screen wide>
      <BackLink href="/admin/businesses">{t("businessesTitle")}</BackLink>
      {failure && <Banner tone="error">{failure}</Banner>}
      {business && (
        <>
          <Heading>{business.name}</Heading>
          <dl className={admin.facts}>
            {[
              [t("slug"), <span key="slug" className={admin.mono}>{business.slug}</span>],
              [t("country"), new Intl.DisplayNames([locale], { type: "region" }).of(business.country) ?? business.country],
              [t("currency"), business.currency],
              [t("created"), shortDate(business.created_at, day)],
              [t("stripeLabel"), t(`stripe.${business.stripe}`)],
            ].map(([label, value], i) => (
              <div key={i} className={admin.fact}>
                <dt>{label}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>

          <section className={admin.section} aria-labelledby="members">
            <h2 className={admin.sectionTitle} id="members">
              {t("membersTitle")}
            </h2>
            <ul className={styles.list}>
              {business.members.map((member) => (
                <li key={member.user_id} className={styles.rowStatic}>
                  <span className={styles.rowMain}>
                    <span className={styles.rowTitle}>{member.name ?? t("unnamed")}</span>
                  </span>
                  <span className={styles.pill}>{t(`role.${member.role}`)}</span>
                </li>
              ))}
            </ul>
          </section>

          <section className={admin.section} aria-labelledby="settings">
            <h2 className={admin.sectionTitle} id="settings">
              {t("settingsTitle")}
            </h2>
            {/* The stored keys as the API names them: an admin tool, and every new key shows without a release here. */}
            <dl className={admin.facts}>
              {Object.entries(business.settings).map(([key, value]) => (
                <div key={key} className={admin.fact}>
                  <dt className={admin.mono}>{key}</dt>
                  <dd>{settingText(value, t("yes"), t("no"))}</dd>
                </div>
              ))}
            </dl>
          </section>

          <section className={admin.section} aria-labelledby="events">
            <h2 className={admin.sectionTitle} id="events">
              {t("eventsTitle")}
            </h2>
            {business.events.length === 0 ? (
              <p className={styles.hint}>{t("noEvents")}</p>
            ) : (
              <ul className={styles.list}>
                {business.events.map((event) => (
                  <li key={event.id} className={styles.rowStatic}>
                    <span className={styles.rowMain}>
                      <span className={`${styles.rowTitle} ${admin.mono}`}>{event.action}</span>
                      <span className={styles.rowMeta}>
                        {[moment.format(new Date(event.created_at)), event.actor_user_id && names.get(event.actor_user_id), event.target_kind]
                          .filter(Boolean)
                          .join(" · ")}
                      </span>
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </>
      )}
    </Screen>
  );
}
