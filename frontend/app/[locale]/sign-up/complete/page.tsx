import { connection } from "next/server";
import { getTranslations } from "next-intl/server";

import { siteKeyFrom } from "@/lib/account";

import { Screen } from "../../_ui/parts";
import { CompleteSignUp } from "./complete-sign-up";

export async function generateMetadata() {
  const t = await getTranslations("CompleteSignUp");
  return { title: t("title") };
}

export default async function CompleteSignUpPage() {
  // Read per request: the site key is runtime configuration, not part of the build.
  await connection();
  return (
    <Screen>
      <CompleteSignUp turnstileSiteKey={siteKeyFrom(process.env.ZIF_TURNSTILE_SITE_KEY)} />
    </Screen>
  );
}
