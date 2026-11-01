"use client";

import { useTranslations } from "next-intl";

import { Link, useRouter } from "@/i18n/navigation";

import { AccountSignIn } from "../../_ui/account-sign-in";
import { NoScript } from "../../_ui/parts";
import styles from "../../_ui/ui.module.css";

export function AccountSignInPage({ turnstileSiteKey }: { turnstileSiteKey: string | null }) {
  const t = useTranslations("SignIn");
  const form = useTranslations("Form");
  const router = useRouter();
  return (
    <AccountSignIn
      turnstileSiteKey={turnstileSiteKey}
      onSignedIn={() => router.replace("/account")}
      aside={
        <p className={styles.aside}>
          {t("newHere")} <Link href="/account/sign-up">{t("createAccount")}</Link>
        </p>
      }
      intro={
        <>
          <h1 className={styles.heading}>{t("title")}</h1>
          <NoScript>{form("needsJavaScript")}</NoScript>
        </>
      }
    />
  );
}
