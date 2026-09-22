import { getTranslations } from "next-intl/server";

import { Calendar } from "../sections";

export async function generateMetadata() {
  const t = await getTranslations("Console.nav");
  return { title: t("calendar") };
}

export default function CalendarPage() {
  return <Calendar />;
}
