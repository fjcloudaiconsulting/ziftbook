import { connection } from "next/server";
import { getTranslations } from "next-intl/server";

import { siteKeyFrom } from "@/lib/account";

import { AccountButton } from "../_ui/account-button";
import { Header } from "../_ui/header";
import styles from "../_ui/ui.module.css";
import { YourBookings } from "./your-bookings";

export async function generateMetadata() {
  const t = await getTranslations("ClientHome");
  return { title: t("title") };
}

/** ZIF-146: the person's own bookings, at every business. Never linked from the console; nothing about the person
 * is rendered on the server (the page reads its data in the browser, with the account cookie). */
export default async function AccountPage() {
  // Read per request: the site key is runtime configuration, not part of the build.
  await connection();
  return (
    <>
      <Header end={<AccountButton signInAfter />} />
      <main className={styles.screen}>
        <div className={`${styles.col} ${styles.colWide}`}>
          <YourBookings turnstileSiteKey={siteKeyFrom(process.env.ZIF_TURNSTILE_SITE_KEY)} />
        </div>
      </main>
    </>
  );
}
