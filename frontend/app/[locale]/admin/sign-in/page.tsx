import { getTranslations } from "next-intl/server";

import { AdminSignIn } from "./admin-sign-in";

export async function generateMetadata() {
  const t = await getTranslations("Admin");
  return { title: t("title"), robots: { index: false } };
}

export default function AdminSignInPage() {
  return <AdminSignIn />;
}
