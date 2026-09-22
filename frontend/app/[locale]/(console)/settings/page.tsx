import { getTranslations } from "next-intl/server";

import { Settings } from "../sections";

export async function generateMetadata() {
  const t = await getTranslations("Console.nav");
  return { title: t("settings") };
}

export default function SettingsPage() {
  return <Settings />;
}
