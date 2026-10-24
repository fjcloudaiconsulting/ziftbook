import { connection } from "next/server";
import { getTranslations } from "next-intl/server";

import { siteKeyFrom } from "@/lib/account";

import { Screen } from "../_ui/parts";
import { AcceptInvite } from "./accept-invite";

export async function generateMetadata() {
  const t = await getTranslations("Invite");
  return { title: t("title") };
}

export default async function AcceptInvitePage() {
  // Read per request: the site key is runtime configuration, not part of the build.
  await connection();
  return (
    <Screen>
      <AcceptInvite turnstileSiteKey={siteKeyFrom(process.env.ZIF_TURNSTILE_SITE_KEY)} />
    </Screen>
  );
}
