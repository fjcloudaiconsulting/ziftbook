import { getTranslations } from "next-intl/server";

import { Link } from "@/i18n/navigation";

import { Outcome, Screen } from "./_ui/parts";
import styles from "./_ui/ui.module.css";

export async function generateMetadata() {
  const t = await getTranslations("NotFound");
  return { title: t("title") };
}

// Every notFound() under [locale], unknown booking slugs and unknown addresses alike (ZIF-148). The
// public header, never the console shell: this renders on the server, before anything knows who is
// signed in.
export default async function NotFound() {
  const t = await getTranslations("NotFound");
  return (
    <Screen>
      <Outcome icon="search" title={t("heading")} lede={t("lede")}>
        <p className={styles.hint}>{t("hint")}</p>
        <Link className={`${styles.button} ${styles.primary}`} href="/">
          {t("home")}
        </Link>
      </Outcome>
    </Screen>
  );
}
