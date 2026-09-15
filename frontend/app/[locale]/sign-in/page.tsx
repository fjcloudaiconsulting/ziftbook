import { getTranslations } from "next-intl/server";

import { SignInForm } from "./sign-in-form";

export async function generateMetadata() {
  const t = await getTranslations("SignIn");
  return { title: t("title") };
}

export default function SignInPage() {
  return <SignInForm />;
}
