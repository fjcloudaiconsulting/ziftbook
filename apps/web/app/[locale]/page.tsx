import { useTranslations } from "next-intl";

import { ApiVersion } from "./api-version";
import styles from "./page.module.css";

export default function HomePage() {
  const t = useTranslations("HomePage");

  return (
    <main className={styles.main}>
      <h1 className={styles.title}>ziftbook</h1>
      <p>{t("tagline")}</p>
      <ApiVersion />
    </main>
  );
}
