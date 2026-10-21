"use client";

import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import type { BookingPageOut } from "@/api-client";
import { useRouter } from "@/i18n/navigation";
import { checkoutUrl } from "@/lib/booking-page";
import { formatMoney } from "@/lib/money";
import { type Locale } from "@/lib/services";
import { zoneCity } from "@/lib/week";

import { Banner, Outcome } from "../_ui/parts";
import { ServiceTile } from "../_ui/service-tile";
import styles from "../_ui/ui.module.css";
import type { AppVersion } from "@/lib/version";

import { BookingFooter } from "./footer";
import { CloseIcon, ClockIcon, PinIcon } from "./icons";
import { ServiceRow } from "./service-list";
import { localized, localizedDesc, type Service } from "./shared";

/** One service's dialog: a native modal <dialog> (the console More sheet's mechanics: Esc, backdrop
 * click, focus return). Open means `service` is set, i.e. the URL's ?service= names a listed one. */
function ServiceDialog({
  page,
  service,
  loc,
  locale,
  t,
  onClose,
  onBook,
}: {
  page: BookingPageOut;
  service: Service | null;
  loc: Locale;
  locale: string;
  t: ReturnType<typeof useTranslations>;
  onClose(): void;
  onBook(service: Service): void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  // The last service shown, so the closing dialog keeps its content until the effect below closes it.
  const [kept, setKept] = useState<Service | null>(null);
  if (service && service !== kept) setKept(service);
  const svc = service ?? kept;

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (service && !dialog.open) dialog.showModal();
    if (!service && dialog.open) dialog.close();
  }, [service]);

  const name = svc ? localized(svc.name, loc, page.language as Locale) : null;
  const desc = svc && localizedDesc(svc.description, loc, page.language as Locale);
  return (
    <dialog
      ref={ref}
      className={styles.svcDialog}
      aria-labelledby="svc-dialog-h"
      // Only a close the user caused navigates: when the URL already lost ?service= (Back), the
      // effect above closed the dialog and there is nothing to navigate.
      onClose={() => {
        if (service) onClose();
      }}
      // The body carries the padding, so a click whose target is the <dialog> itself can only land
      // on the backdrop.
      onClick={(event) => {
        if (event.target === ref.current) ref.current?.close();
      }}
    >
      {svc && name && (
        <>
          <button className={`${styles.button} ${styles.secondary} ${styles.small} ${styles.svcAdd} ${styles.svcDialogClose}`} type="button" aria-label={t("close")} onClick={() => ref.current?.close()}>
            <CloseIcon />
          </button>
          {svc.photo ? (
            // eslint-disable-next-line @next/next/no-img-element -- the CDN serves sized variants: no optimiser
            <img className={styles.svcPhoto} src={svc.photo.detail_url} width={svc.photo.width} height={svc.photo.height} alt="" decoding="async" />
          ) : (
            <div className={styles.svcPhotoTile}>
              <ServiceTile placeholder={svc.placeholder} className={styles.tile96} />
            </div>
          )}
          <div className={styles.svcDialogBody}>
            <h2 id="svc-dialog-h" className={styles.checkoutHead} lang={name.lang ?? undefined}>
              {name.text}
            </h2>
            <p className={styles.bizMeta}>
              <PinIcon />
              <span>{zoneCity(page.timezone)}</span>
            </p>
            {desc && <p lang={desc.lang ?? undefined}>{desc.text}</p>}
            <p className={styles.svcMeta}>
              <ClockIcon />
              {t("min", { n: svc.duration_minutes })} · {formatMoney(svc.price.amount_minor, svc.price.currency, locale)}
            </p>
          </div>
          <div className={styles.svcDialogBook}>
            <button className={`${styles.button} ${styles.primary}`} type="button" onClick={() => onBook(svc)}>
              {t("bookNow")}
            </button>
          </div>
        </>
      )}
    </dialog>
  );
}

/** `/[slug]`: the business and its services. The dialog's one source of truth is ?service=. */
export function ServicesPage({ page, locale, version }: { page: BookingPageOut; locale: string; version: AppVersion | null }) {
  const t = useTranslations("BookingPage");
  const router = useRouter();
  const params = useSearchParams();
  const loc = locale as Locale;
  const base = `/${page.slug}`;
  const open = page.services.find((svc) => svc.id === params.get("service")) ?? null;
  // ?gone=1 (a checkout whose service vanished): shown once, then dropped from the address.
  const [gone] = useState(() => params.get("gone") === "1");
  useEffect(() => {
    // The router, not native History: on a hard load this runs before the App Router patches History.
    if (params.has("gone")) router.replace(base, { scroll: false });
    // Mount only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <>
      <main className={styles.screen}>
        <div className={`${styles.col} ${styles.bookingWide}`}>
          <div className={styles.bizHeader}>
            <h1>{page.name}</h1>
            <p className={styles.bizMeta}>
              <PinIcon />
              <span>{zoneCity(page.timezone)}</span>
              <span aria-hidden="true">·</span>
              <span>{t("timesIn", { city: zoneCity(page.timezone) })}</span>
            </p>
          </div>
          {gone && <Banner tone="note">{t("serviceGone")}</Banner>}
          {page.services.length === 0 ? (
            <Outcome icon="calendar" title={t("noSvcTitle")} lede={t("noSvcBody", { biz: page.name })}>
              {null}
            </Outcome>
          ) : (
            <>
              <h2 id="services-h" className={styles.checkoutHead}>
                {t("servicesTitle")}
              </h2>
              <ul className={styles.services} aria-labelledby="services-h">
                {page.services.map((svc) => (
                  <ServiceRow key={svc.id} svc={svc} loc={loc} businessLanguage={page.language as Locale} locale={locale} t={t} onOpen={(s) => window.history.pushState(null, "", `/${locale}${base}?service=${s.id}`)} />
                ))}
              </ul>
            </>
          )}
        </div>
        <ServiceDialog
          page={page}
          service={open}
          loc={loc}
          locale={locale}
          t={t}
          onClose={() => window.history.replaceState(null, "", `/${locale}${base}`)}
          onBook={(svc) => router.replace(checkoutUrl(page.slug, { serviceId: svc.id, memberId: null, at: null }))}
        />
      </main>
      <BookingFooter version={version} />
    </>
  );
}
