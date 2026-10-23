import { notFound, redirect } from "next/navigation";
import { getTranslations } from "next-intl/server";

import { siteKeyFrom } from "@/lib/account";
import { parseCheckout } from "@/lib/booking-page";
import { appVersion } from "@/lib/version";

import { LanguageSwitcher } from "../../_ui/header";
import { Outcome } from "../../_ui/parts";
import styles from "../../_ui/ui.module.css";
import { BookingPage } from "../booking-page";
import { load } from "../load";

export { generateMetadata } from "../page";

/** `/[slug]/book?service=<id>&with=<memberId>&at=<UTC ISO>`: the checkout. An unknown or missing
 * service goes back to the list; `with` and `at` that do not hold up are dropped (parseCheckout). */
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export default async function CheckoutPage({
  params,
  searchParams,
}: {
  params: Promise<{ slug: string; locale: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { slug, locale } = await params;
  const query = await searchParams;
  const loaded = await load(slug);

  if (loaded.status === 404) notFound();

  // Only service/with/at/booking, so nothing else (Stripe's return params) leaks through a retry.
  const pick = new URLSearchParams();
  for (const key of ["service", "with", "at", "booking"]) {
    const value = query[key];
    if (typeof value === "string" && (key !== "booking" || UUID.test(value))) pick.set(key, value);
  }

  if (loaded.status === "error") {
    const retry = pick.has("service") ? `/${slug}/book?${pick}` : `/${slug}/book`;
    const t = await getTranslations("BookingPage");
    return (
      <>
        <header className={styles.minimalHeader}><LanguageSwitcher /></header>
        <main className={styles.screen}>
          <div className={styles.col}>
            <Outcome icon="calendar" title={t("loadErrorTitle")} lede={t("loadErrorLede")}>
              <a className={`${styles.button} ${styles.primary}`} href={`/${locale}${retry}`}>
                {t("tryAgain")}
              </a>
            </Outcome>
          </div>
        </main>
      </>
    );
  }

  const service = loaded.data.services.find((svc) => svc.id === pick.get("service"));
  if (!service) redirect(`/${locale}/${slug}`);
  const { memberId, at } = parseCheckout(pick, service);

  return (
    <>
      <header className={styles.minimalHeader}><LanguageSwitcher /></header>
      <BookingPage
        page={loaded.data}
        locale={locale}
        turnstileSiteKey={siteKeyFrom(process.env.ZIF_TURNSTILE_SITE_KEY)}
        version={appVersion()}
        serviceId={service.id}
        memberId={memberId}
        at={at}
        booking={pick.get("booking")}
      />
    </>
  );
}
