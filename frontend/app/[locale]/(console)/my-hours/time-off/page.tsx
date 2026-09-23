import { getTranslations } from "next-intl/server";

import { MyHours } from "../../my-hours";

export async function generateMetadata() {
  const t = await getTranslations("Console.nav");
  return { title: t("myHours") };
}

export default function MyHoursTimeOffPage() {
  return <MyHours tab="timeOff" />;
}
