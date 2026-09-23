import { getTranslations } from "next-intl/server";

import { NewService } from "../services";

export async function generateMetadata() {
  const t = await getTranslations("Console.services.form");
  return { title: t("newHeading") };
}

export default function NewServicePage() {
  return <NewService />;
}
