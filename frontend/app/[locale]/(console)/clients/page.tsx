import { getTranslations } from "next-intl/server";

import { Clients } from "../sections";

export async function generateMetadata() {
  const t = await getTranslations("Console.nav");
  return { title: t("clients") };
}

export default function ClientsPage() {
  return <Clients />;
}
