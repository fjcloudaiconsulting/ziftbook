import { connection } from "next/server";
import { getTranslations } from "next-intl/server";

import { siteKeyFrom } from "@/lib/account";

import { AccountButton } from "../../_ui/account-button";
import { Screen } from "../../_ui/parts";
import { CompleteAccount } from "./complete-account";

export async function generateMetadata() {
  const t = await getTranslations("ClientSignUp");
  return { title: t("completeTitle") };
}

/** ZIF-146: the account sign-up link opens here, its token in the fragment (never sent to the server). The page
 * loads no third-party script until the person asks for a new link (security B3.7). */
export default async function CompleteAccountPage() {
  // Read per request: the site key is runtime configuration, not part of the build.
  await connection();
  return (
    <Screen headerEnd={<AccountButton signInAfter />}>
      <CompleteAccount turnstileSiteKey={siteKeyFrom(process.env.ZIF_TURNSTILE_SITE_KEY)} />
    </Screen>
  );
}
