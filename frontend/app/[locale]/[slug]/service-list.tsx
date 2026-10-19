"use client";

import type { useTranslations } from "next-intl";
import { useState } from "react";

import { formatMoney } from "@/lib/money";
import type { Locale } from "@/lib/services";

import { ServiceTile } from "../_ui/service-tile";
import styles from "../_ui/ui.module.css";
import { ClockIcon, PlusIcon } from "./icons";
import { localized, localizedDesc, type Service } from "./shared";

/** One row of the open list of services: it opens that service's dialog (no radios). */
export function ServiceRow({
  svc,
  loc,
  businessLanguage,
  locale,
  t,
  onOpen,
}: {
  svc: Service;
  loc: Locale;
  businessLanguage: Locale;
  locale: string;
  t: ReturnType<typeof useTranslations>;
  onOpen(service: Service): void;
}) {
  const name = localized(svc.name, loc, businessLanguage);
  const desc = localizedDesc(svc.description, loc, businessLanguage);
  const [expanded, setExpanded] = useState(false);
  // Sticky once seen clamped: expanded text no longer overflows, but "Show less" must stay.
  const [clamped, setClamped] = useState(false);
  return (
    <li>
      <div className={styles.svcChoice} onClick={() => onOpen(svc)}>
        <ServiceTile placeholder={svc.placeholder} photo={svc.photo?.thumb_url} />
        <span className={styles.svcMain}>
          <span className={styles.svcName} lang={name.lang ?? undefined}>
            {name.text}
          </span>
          {desc && (
            <>
              <span
                className={`${styles.svcDesc} ${expanded ? "" : styles.svcClamp}`}
                lang={desc.lang ?? undefined}
                ref={(el) => {
                  if (el && el.scrollHeight > el.clientHeight + 1) setClamped(true);
                }}
              >
                {desc.text}
              </span>
              {(clamped || expanded) && (
                <button
                  className={styles.textButton}
                  type="button"
                  aria-expanded={expanded}
                  onClick={(event) => {
                    event.stopPropagation();
                    setExpanded(!expanded);
                  }}
                >
                  {expanded ? t("showLess") : t("showMore")}
                </button>
              )}
            </>
          )}
          <span className={styles.svcMeta}>
            <ClockIcon />
            {t("min", { n: svc.duration_minutes })} · {formatMoney(svc.price.amount_minor, svc.price.currency, locale)}
          </span>
        </span>
        <button
          className={`${styles.button} ${styles.secondary} ${styles.small} ${styles.svcAdd}`}
          type="button"
          aria-label={t("openService", { service: name.text })}
          onClick={(event) => {
            event.stopPropagation();
            onOpen(svc);
          }}
        >
          <PlusIcon />
        </button>
      </div>
    </li>
  );
}
