import type { useTranslations } from "next-intl";

import type { BookingPageOut } from "@/api-client";
import { formatMoney } from "@/lib/money";
import type { Locale } from "@/lib/services";

import { ServiceTile } from "../_ui/service-tile";
import styles from "../_ui/ui.module.css";
import { ClockIcon } from "./icons";
import { localized, type Service } from "./shared";

/** Step 1's open list of services. */
export function ServiceList({
  page,
  selected,
  loc,
  businessLanguage,
  locale,
  t,
  onSelect,
}: {
  page: BookingPageOut;
  selected: string | undefined;
  loc: Locale;
  businessLanguage: Locale;
  locale: string;
  t: ReturnType<typeof useTranslations>;
  onSelect(service: Service): void;
}) {
  return (
    <ul className={styles.services} role="radiogroup" aria-labelledby="step1-h">
      {page.services.map((svc) => {
        const name = localized(svc.name, loc, businessLanguage);
        const desc = svc.description[loc] || svc.description[businessLanguage] ? localized(svc.description, loc, businessLanguage) : null;
        return (
          <li key={svc.id}>
            <label className={styles.svcChoice}>
              <input type="radio" name="service" checked={selected === svc.id} readOnly onClick={() => onSelect(svc)} />
              <ServiceTile placeholder={svc.placeholder} photo={svc.photo?.thumb_url} />
              <span className={styles.svcMain}>
                <span className={styles.svcTop}>
                  <span className={styles.svcName} lang={name.lang ?? undefined}>
                    {name.text}
                  </span>
                  <span className={styles.svcPrice}>{formatMoney(svc.price.amount_minor, svc.price.currency, locale)}</span>
                </span>
                {desc && (
                  <span className={styles.svcDesc} lang={desc.lang ?? undefined}>
                    {desc.text}
                  </span>
                )}
                <span className={styles.svcMeta}>
                  <ClockIcon />
                  {t("min", { n: svc.duration_minutes })}
                </span>
              </span>
            </label>
          </li>
        );
      })}
    </ul>
  );
}
