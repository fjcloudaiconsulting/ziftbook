import { getTranslations } from "next-intl/server";

import { MyHoursTimeOff } from "../../my-hours";

export async function generateMetadata() {
  const t = await getTranslations("Console.nav");
  return { title: t("myHours") };
}

export default function MyHoursTimeOffPage() {
  return <MyHoursTimeOff />;
}
