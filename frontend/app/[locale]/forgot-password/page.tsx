import { useTranslations } from "next-intl";
import { getTranslations } from "next-intl/server";

import { Link } from "@/i18n/navigation";

import { LinkRequest } from "../_ui/link-request";
import { Screen } from "../_ui/parts";
import styles from "../_ui/ui.module.css";

export async function generateMetadata() {
  const t = await getTranslations("ForgotPassword");
  return { title: t("title") };
}

export default function ForgotPasswordPage() {
  const t = useTranslations("ForgotPassword");
  return (
    <Screen>
      <LinkRequest
        purpose="password_reset"
        intro={
          <>
            <h1 className={styles.heading}>{t("title")}</h1>
            <p className={styles.lede}>{t("lede")}</p>
          </>
        }
        submit={t("submit")}
        aside={
          <p className={styles.aside}>
            <Link href="/sign-in">{t("back")}</Link>
          </p>
        }
      />
    </Screen>
  );
}
