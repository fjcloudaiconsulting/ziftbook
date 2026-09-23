import { getTranslations } from "next-intl/server";

import { TeamList } from "./team";

export async function generateMetadata() {
  const t = await getTranslations("Console.nav");
  return { title: t("team") };
}

export default function TeamPage() {
  return <TeamList />;
}
