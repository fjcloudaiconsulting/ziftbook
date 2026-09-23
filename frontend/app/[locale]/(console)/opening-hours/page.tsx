import { getTranslations } from "next-intl/server";

import { OpeningHours } from "../opening-hours";

export async function generateMetadata() {
  const t = await getTranslations("Console.nav");
  return { title: t("openingHours") };
}

export default function OpeningHoursPage() {
  return <OpeningHours />;
}
