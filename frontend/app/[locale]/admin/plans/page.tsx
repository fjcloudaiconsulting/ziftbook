import { getTranslations } from "next-intl/server";

import { Plans } from "./plans";

export async function generateMetadata() {
  const t = await getTranslations("Admin");
  return { title: t("plans.title"), robots: { index: false } };
}

export default function PlansPage() {
  return <Plans />;
}
