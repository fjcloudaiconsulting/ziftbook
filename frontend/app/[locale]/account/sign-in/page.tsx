import { connection } from "next/server";
import { getTranslations } from "next-intl/server";

import { siteKeyFrom } from "@/lib/account";

import { AccountButton } from "../../_ui/account-button";
import { Screen } from "../../_ui/parts";
import { AccountSignInPage } from "./account-sign-in-page";

export async function generateMetadata() {
  const t = await getTranslations("SignIn");
  return { title: t("title") };
}

/** ZIF-146: a client's own account, with no business. The "you already have an account" email links here. */
export default async function SignInPage() {
  // Read per request: the site key is runtime configuration, not part of the build.
  await connection();
  return (
    <Screen headerEnd={<AccountButton signInAfter />}>
      <AccountSignInPage turnstileSiteKey={siteKeyFrom(process.env.ZIF_TURNSTILE_SITE_KEY)} />
    </Screen>
  );
}
