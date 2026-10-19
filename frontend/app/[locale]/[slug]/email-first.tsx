"use client";

import { useTranslations } from "next-intl";

import type { BookingPageOut } from "@/api-client";
import { localWhen } from "@/lib/booking-link";
import { formatMoney } from "@/lib/money";
import { type Locale } from "@/lib/services";
import { zoneCity } from "@/lib/week";

import { Banner, Outcome } from "../_ui/parts";
import styles from "../_ui/ui.module.css";
import { Held } from "./checkout";
import { localized, type Service } from "./shared";

// ZIF-117: the "Check your email" screen. `secret` (the hold POST's answer) lives in this page's
// memory only; it is what replaces or releases the hold, so nobody else can.
export type Sent = { secret: string; email: string; startsAt: string; heldUntil: string; service: Service; who: string | null; notice: { tone: "note" | "error"; text: string } | null };

/** ZIF-117 "Check your email": replaces the done screen for anyone the link is mailed to. It never
 * learns about the click (no polling), which is why it says the page can be closed. */
export function CheckEmail({
  page,
  sent,
  locale,
  dLocale,
  zone,
  t,
  busy,
  verifying,
  banner,
  onAgain,
  onOtherTime,
  onWrongAddress,
}: {
  page: BookingPageOut;
  sent: Sent;
  locale: Locale;
  dLocale: string;
  zone: string;
  t: ReturnType<typeof useTranslations>;
  busy: boolean;
  verifying: boolean;
  banner: { tone: "note" | "error"; text: string } | null;
  onAgain(): void;
  onOtherTime(): void;
  onWrongAddress(): void;
}) {
  const end = new Date(Date.parse(sent.startsAt) + sent.service.duration_minutes * 60_000);
  const w = localWhen(sent.startsAt, zone, dLocale);
  const endTime = new Intl.DateTimeFormat(dLocale, { hour: "2-digit", minute: "2-digit", timeZone: zone }).format(end);
  const until = localWhen(sent.heldUntil, zone, dLocale).time;
  const name = localized(sent.service.name, locale, page.language as Locale);
  return (
    <div className={styles.outcomeGrid}>
      <Outcome icon="mail" title={t("checkTitle")} lede={t("checkLede", { email: sent.email })}>
        {sent.notice && <Banner tone={sent.notice.tone}>{sent.notice.text}</Banner>}
        {verifying && <Banner tone="note">{t("verifying")}</Banner>}
        {banner && <Banner tone={banner.tone}>{banner.text}</Banner>}
        <ul className={styles.doneList}>
          <li lang={name.lang ?? undefined}>{name.text}</li>
          <li>
            {t("whenFormat", { day: w.date, from: w.time, to: endTime })} {"("}
            {zoneCity(zone)}
            {")"}
          </li>
          <li>
            {sent.who && `${sent.who} · `}
            {formatMoney(sent.service.price.amount_minor, sent.service.price.currency, locale)}
          </li>
        </ul>
        <Held>{t("checkHold", { time: w.time, until })}</Held>
        <p className={styles.hint}>{t("checkHint")}</p>
        <div className={styles.btnRow}>
          <button className={`${styles.button} ${styles.secondary}`} type="button" disabled={busy} onClick={onAgain}>
            {t("sendAgain")}
          </button>
          <button className={`${styles.button} ${styles.secondary}`} type="button" disabled={busy} onClick={onOtherTime}>
            {t("otherTime")}
          </button>
        </div>
        <button className={styles.textButton} type="button" onClick={onWrongAddress}>
          {t("wrongAddress")}
        </button>
      </Outcome>
    </div>
  );
}
