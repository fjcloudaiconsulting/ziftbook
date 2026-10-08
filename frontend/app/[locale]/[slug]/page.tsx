import { headers } from "next/headers";
import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";
import { cache } from "react";

import type { BookingPageOut } from "@/api-client/types.gen";
import { slugLooksValid } from "@/lib/booking-page";
import { apiUrl, clientIp } from "@/lib/upstream";

import { LanguageSwitcher } from "../_ui/header";
import { Outcome } from "../_ui/parts";
import styles from "../_ui/ui.module.css";
import { BookingPage } from "./booking-page";

type Loaded = { status: 200; data: BookingPageOut } | { status: 404 } | { status: "error" };

// cache(): one GET per request, shared by generateMetadata and the page itself (React's request
// dedup, not a cross-request cache — "no-store" below still forbids caching between requests).
const load = cache(async (slug: string): Promise<Loaded> => {
  // Same shape the backend itself refuses (booking_page.py): never spend a request, or a rate-limit
  // hit, on a slug that could not possibly be stored.
  if (!slugLooksValid(slug)) return { status: 404 };
  const upstream = apiUrl();
  if (!upstream) return { status: "error" };
  // Set exactly as proxy.ts forwards it: the API's rate limiting and access logs see the visitor,
  // not this server.
  const ip = clientIp(await headers());
  try {
    const response = await fetch(new URL(`/api/public/booking-pages/${encodeURIComponent(slug)}`, upstream), {
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

// A fixed, generic title whenever the fetch didn't come back 200: never the upstream error body,
// and never presented as if the page were real (search engines / link previews should not treat a
// 404 or a failed load as a named business).
const FALLBACK_TITLE = "ziftbook";

export async function generateMetadata({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  const loaded = await load(slug);
  return { title: loaded.status === 200 ? loaded.data.name : FALLBACK_TITLE };
}

export default async function PublicBookingPage({
  params,
  searchParams,
}: {
  params: Promise<{ slug: string; locale: string }>;
  searchParams: Promise<{ service?: string | string[] }>;
}) {
  const { slug, locale } = await params;
  // ZIF-117: "Choose another time" from the confirm page opens this service's times.
  const { service } = await searchParams;
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
      <BookingPage
        page={loaded.data}
        locale={locale}
        turnstileSiteKey={process.env.ZIF_TURNSTILE_SITE_KEY ?? null}
        initialService={typeof service === "string" ? service : null}
      />
    </>
  );
}
