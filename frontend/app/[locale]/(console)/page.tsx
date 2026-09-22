import { getTranslations } from "next-intl/server";

import { Today } from "./sections";

export async function generateMetadata() {
  const t = await getTranslations("Console.nav");
  return { title: t("today") };
}

export default function TodayPage() {
  return <Today />;
}
