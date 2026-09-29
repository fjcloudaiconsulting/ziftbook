import { headers } from "next/headers";
import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";
import { cache } from "react";

import type { BookingPageOut } from "@/api-client/types.gen";
import { apiUrl, clientIp } from "@/lib/upstream";

import { Header } from "../_ui/header";
import { Outcome } from "../_ui/parts";
import styles from "../_ui/ui.module.css";
import { BookingPage } from "./booking-page";

type Loaded = { status: 200; data: BookingPageOut } | { status: 404 } | { status: "error" };

// cache(): one GET per request, shared by generateMetadata and the page itself (React's request
// dedup, not a cross-request cache — "no-store" below still forbids caching between requests).
const load = cache(async (slug: string): Promise<Loaded> => {
  const upstream = apiUrl();
  if (!upstream) return { status: "error" };
  // Set exactly as proxy.ts forwards it: the API's rate limiting and access logs see the visitor,
  // not this server.
  const ip = clientIp(await headers());
  try {
    const response = await fetch(new URL(`/api/public/booking-pages/${slug}`, upstream), {
      cache: "no-store",
      headers: ip ? { "x-forwarded-for": ip } : undefined,
    });
    if (response.status === 404) return { status: 404 };
    if (!response.ok) return { status: "error" };
    return { status: 200, data: (await response.json()) as BookingPageOut };
  } catch {
    return { status: "error" };
  }
});

export async function generateMetadata({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  const loaded = await load(slug);
  return { title: loaded.status === 200 ? loaded.data.name : slug };
}

export default async function PublicBookingPage({ params }: { params: Promise<{ slug: string; locale: string }> }) {
  const { slug, locale } = await params;
  const loaded = await load(slug);

  if (loaded.status === 404) notFound();

  if (loaded.status === "error") {
    const t = await getTranslations("BookingPage");
    return (
      <>
        <Header />
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
      <Header />
      <BookingPage page={loaded.data} locale={locale} turnstileSiteKey={process.env.ZIF_TURNSTILE_SITE_KEY ?? null} />
    </>
  );
}
