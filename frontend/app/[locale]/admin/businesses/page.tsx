import { getTranslations } from "next-intl/server";

import { Businesses } from "./businesses";

export async function generateMetadata() {
  const t = await getTranslations("Admin");
  return { title: t("businessesTitle"), robots: { index: false } };
}

export default function BusinessesPage() {
  return <Businesses />;
}
