import { getTranslations } from "next-intl/server";

import { Services } from "./services";

export async function generateMetadata() {
  const t = await getTranslations("Console.nav");
  return { title: t("services") };
}

export default function ServicesPage() {
  return <Services />;
}
