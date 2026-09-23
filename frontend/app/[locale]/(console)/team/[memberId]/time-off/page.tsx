import { getTranslations } from "next-intl/server";

import { PersonTimeOff } from "../../team";

export async function generateMetadata() {
  const t = await getTranslations("Console.nav");
  return { title: t("team") };
}

export default async function PersonTimeOffPage({ params }: { params: Promise<{ memberId: string }> }) {
  const { memberId } = await params;
  return <PersonTimeOff memberId={memberId} />;
}
