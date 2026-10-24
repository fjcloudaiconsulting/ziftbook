import { connection } from "next/server";
import { getTranslations } from "next-intl/server";

import { siteKeyFrom } from "@/lib/account";

import { AdminSignIn } from "./admin-sign-in";

export async function generateMetadata() {
  const t = await getTranslations("Admin");
  return { title: t("title"), robots: { index: false } };
}

export default async function AdminSignInPage() {
  // Read per request: the site key is runtime configuration, not part of the build.
  await connection();
  return <AdminSignIn turnstileSiteKey={siteKeyFrom(process.env.ZIF_TURNSTILE_SITE_KEY)} />;
}
