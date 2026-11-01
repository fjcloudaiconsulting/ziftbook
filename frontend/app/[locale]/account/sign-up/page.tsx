import { connection } from "next/server";
import { getTranslations } from "next-intl/server";

import { Link } from "@/i18n/navigation";
import { siteKeyFrom } from "@/lib/account";

import { LinkRequest } from "../../_ui/link-request";
import { AccountButton } from "../../_ui/account-button";
import { Screen } from "../../_ui/parts";
import styles from "../../_ui/ui.module.css";

export async function generateMetadata() {
  const t = await getTranslations("ClientSignUp");
  return { title: t("title") };
}

/** ZIF-146: a client's own account, email first. The screen after it is the same whether or not the email already
 * has an account: that person gets a "you already have an account" email instead of a link. */
export default async function ClientSignUpPage() {
  // Read per request: the site key is runtime configuration, not part of the build.
  await connection();
  const t = await getTranslations("ClientSignUp");
  const signUp = await getTranslations("SignUp");
  return (
    <Screen headerEnd={<AccountButton signInAfter />}>
      <LinkRequest
        purpose="account_sign_up"
        intro={
          <>
            <h1 className={styles.heading}>{t("title")}</h1>
            <p className={styles.lede}>{t("lede")}</p>
          </>
        }
        turnstileSiteKey={siteKeyFrom(process.env.ZIF_TURNSTILE_SITE_KEY)}
        submit={signUp("submit")}
        aside={
          <p className={styles.aside}>
            {signUp("haveAccount")} <Link href="/account/sign-in">{signUp("signIn")}</Link>
          </p>
        }
      />
    </Screen>
  );
}
