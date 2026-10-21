import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";

import { appVersion } from "@/lib/version";

import { LanguageSwitcher } from "../_ui/header";
import { Outcome } from "../_ui/parts";
import styles from "../_ui/ui.module.css";
import { load } from "./load";
import { ServicesPage } from "./services-page";

// A fixed, generic title when the load failed: never the upstream error body, and never presented
// as if the page were real (search engines / link previews should not treat a failed load as a
// named business).
const FALLBACK_TITLE = "ziftbook";

export async function generateMetadata({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  const loaded = await load(slug);
  // A 404 takes not-found's title (ZIF-148); returning one here would replace it in the browser.
  if (loaded.status === 404) notFound();
  return { title: loaded.status === 200 ? loaded.data.name : FALLBACK_TITLE };
}

export default async function PublicBookingPage({
  params,
}: {
  params: Promise<{ slug: string; locale: string }>;
}) {
  const { slug, locale } = await params;
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
              <a className={`${styles.button} ${styles.primary}`} href={`/${locale}/${slug}`}>
                {t("tryAgain")}
              </a>
            </Outcome>
          </div>
        </main>
      </>
    );
  }

  return (
    <>
      <header className={styles.minimalHeader}><LanguageSwitcher /></header>
      <ServicesPage page={loaded.data} locale={locale} version={appVersion()} />
    </>
  );
}
