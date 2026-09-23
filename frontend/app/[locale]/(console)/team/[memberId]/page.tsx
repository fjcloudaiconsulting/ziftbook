import { getTranslations } from "next-intl/server";

import { Person } from "../team";

export async function generateMetadata() {
  const t = await getTranslations("Console.nav");
  return { title: t("team") };
}

export default async function PersonPage({ params }: { params: Promise<{ memberId: string }> }) {
  const { memberId } = await params;
  return <Person memberId={memberId} />;
}
