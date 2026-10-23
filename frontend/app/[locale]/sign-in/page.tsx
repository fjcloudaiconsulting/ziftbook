import { connection } from "next/server";
import { getTranslations } from "next-intl/server";

import { siteKeyFrom } from "@/lib/account";

import { SignInForm } from "./sign-in-form";

export async function generateMetadata() {
  const t = await getTranslations("SignIn");
  return { title: t("title") };
}

export default async function SignInPage() {
  // Read per request: the site key is runtime configuration, not part of the build.
  await connection();
  return <SignInForm turnstileSiteKey={siteKeyFrom(process.env.ZIF_TURNSTILE_SITE_KEY)} />;
}
