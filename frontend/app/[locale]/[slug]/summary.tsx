"use client";

import type { useTranslations } from "next-intl";
import type { RefObject } from "react";

import type { BookingPageOut } from "@/api-client";
import { localWhen } from "@/lib/booking-link";
import { formatMoney } from "@/lib/money";
import { type Locale } from "@/lib/services";

import { Banner, FieldError } from "../_ui/parts";
import { ServiceTile } from "../_ui/service-tile";
import styles from "../_ui/ui.module.css";
import { Terms } from "./checkout";
import { localized, type Service } from "./shared";

/** The checkout page's aside: the service, the time and person once picked, the booker's optional
 * message for the business, the total and the cancellation terms. */
export function Summary({
  page,
  service,
  slot,
  workerName,
  locale,
  dLocale,
  zone,
  t,
  message,
  messageOpen,
  messageError,
  messageRef,
  extraTerms,
  locked,
  onMessage,
  onMessageOpen,
}: {
  page: Pick<BookingPageOut, "name" | "cancellation" | "auto_confirm" | "language">;
  service: Service;
  slot: string | null;
  /** null: a person with no name, so no "With" row at all. */
  workerName: string | null;
  locale: string;
  dLocale: string;
  zone: string;
  t: ReturnType<typeof useTranslations>;
  message: string;
  messageOpen: boolean;
  messageError: string | undefined;
  messageRef: RefObject<HTMLTextAreaElement | null>;
  /** termsLines: further lines for the terms block. */
  extraTerms: "termsPaymentFee"[];
  /** The request is sent and awaits payment: the message can no longer change. */
  locked: boolean;
  /** null: no message box at all (the resume view only reads the booking). */
  onMessage: ((text: string) => void) | null;
  onMessageOpen(open: boolean): void;
}) {
  const businessLanguage = page.language as Locale;
  const name = localized(service.name, locale as Locale, businessLanguage);
  const price = formatMoney(service.price.amount_minor, service.price.currency, locale);
  let when: string | null = null;
  if (slot) {
    const w = localWhen(slot, zone, dLocale);
    const endTime = new Intl.DateTimeFormat(dLocale, { hour: "2-digit", minute: "2-digit", timeZone: zone }).format(new Date(Date.parse(slot) + service.duration_minutes * 60_000));
    when = t("whenFormat", { day: w.date, from: w.time, to: endTime });
  }
  return (
    <aside className={styles.checkout} aria-labelledby="checkout-h">
      <h2 id="checkout-h" className={styles.checkoutHead}>
        {t("yourBooking")}
      </h2>
      <div className={styles.sumHead}>
        <ServiceTile placeholder={service.placeholder} photo={service.photo?.thumb_url} className={styles.tile40} />
        <span className={styles.stepDoneWhat}>
          <b lang={name.lang ?? undefined}>{name.text}</b>
          <span>
            {t("min", { n: service.duration_minutes })} · {price}
          </span>
        </span>
      </div>
      {when !== null && (
        <dl className={styles.summary}>
          <div className={styles.summaryRow}>
            <dt>{t("when")}</dt>
            <dd>{when}</dd>
          </div>
          {workerName !== null && (
            <div className={styles.summaryRow}>
              <dt>{t("withRow")}</dt>
              <dd>{workerName}</dd>
            </div>
          )}
        </dl>
      )}
      {onMessage && (
        <details open={messageOpen} onToggle={(event) => onMessageOpen(event.currentTarget.open)}>
          <summary className={styles.textButton}>{t("message", { biz: page.name })}</summary>
          <div className={styles.field}>
            <div className={styles.input}>
              <textarea
                ref={messageRef}
                name="message"
                maxLength={1000}
                aria-label={t("message", { biz: page.name })}
                value={message}
                readOnly={locked}
                onChange={(e) => onMessage(e.target.value)}
                aria-invalid={messageError ? true : undefined}
                aria-describedby={messageError ? "bp-message-hint bp-message-err" : "bp-message-hint"}
              />
            </div>
            <p className={styles.hint} id="bp-message-hint">
              {t("messageHint", { biz: page.name })}
            </p>
            {messageError && <FieldError id="bp-message-err">{messageError}</FieldError>}
          </div>
        </details>
      )}
      <dl className={styles.summary}>
        <div className={`${styles.summaryRow} ${styles.summaryTotal}`}>
          <dt>{t("total")}</dt>
          <dd>{price}</dd>
        </div>
      </dl>
      <Terms page={page} slot={slot} locale={locale} dLocale={dLocale} zone={zone} businessLanguage={businessLanguage} extra={extraTerms} t={t} />
      {!page.auto_confirm && <Banner tone="note">{t("approvalNote", { biz: page.name })}</Banner>}
    </aside>
  );
}
