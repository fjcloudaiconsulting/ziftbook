import { useTranslations } from "next-intl";
import { getTranslations } from "next-intl/server";

import { Link } from "@/i18n/navigation";

import { LinkRequest } from "../_ui/link-request";
import { Screen } from "../_ui/parts";
import styles from "../_ui/ui.module.css";

export async function generateMetadata() {
  const t = await getTranslations("SignUp");
  return { title: t("title") };
}

export default function SignUpPage() {
  const t = useTranslations("SignUp");
  return (
    <Screen>
      <LinkRequest
        purpose="sign_up"
        intro={
          <>
            <h1 className={styles.heading}>{t("heading")}</h1>
            <p className={styles.lede}>{t("lede")}</p>
          </>
        }
        submit={t("submit")}
        aside={
          <p className={styles.aside}>
            {t("haveAccount")} <Link href="/sign-in">{t("signIn")}</Link>
          </p>
        }
      />
    </Screen>
  );
}
