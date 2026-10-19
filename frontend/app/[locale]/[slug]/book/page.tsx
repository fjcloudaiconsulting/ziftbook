import { notFound, redirect } from "next/navigation";
import { getTranslations } from "next-intl/server";

import { parseCheckout } from "@/lib/booking-page";

import { LanguageSwitcher } from "../../_ui/header";
import { Outcome } from "../../_ui/parts";
import styles from "../../_ui/ui.module.css";
import { BookingPage } from "../booking-page";
import { load } from "../load";

// A fixed, generic title when the load failed, as the services page has.
const FALLBACK_TITLE = "ziftbook";

export async function generateMetadata({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  const loaded = await load(slug);
  if (loaded.status === 404) notFound();
  return { title: loaded.status === 200 ? loaded.data.name : FALLBACK_TITLE };
}

/** `/[slug]/book?service=<id>&with=<memberId>&at=<UTC ISO>`: the checkout. An unknown or missing
 * service goes back to the list; `with` and `at` that do not hold up are dropped (parseCheckout). */
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

  if (loaded.status === "error") {
    const t = await getTranslations("BookingPage");
    return (
      <>
        <header className={styles.minimalHeader}><LanguageSwitcher /></header>
        <main className={styles.screen}>
          <div className={styles.col}>
            <Outcome icon="calendar" title={t("loadErrorTitle")} lede={t("loadErrorLede")}>
              <a className={`${styles.button} ${styles.primary}`} href={`/${locale}/${slug}/book`}>
                {t("tryAgain")}
              </a>
            </Outcome>
          </div>
        </main>
      </>
    );
  }

  const pick = new URLSearchParams();
  for (const key of ["service", "with", "at"]) {
    const value = query[key];
    if (typeof value === "string") pick.set(key, value);
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
        turnstileSiteKey={process.env.ZIF_TURNSTILE_SITE_KEY ?? null}
        serviceId={service.id}
        memberId={memberId}
        at={at}
      />
    </>
  );
}
