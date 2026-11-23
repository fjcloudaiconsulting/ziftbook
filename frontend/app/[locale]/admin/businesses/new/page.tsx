import { getTranslations } from "next-intl/server";

import { NewBusiness } from "./new-business";

export async function generateMetadata() {
  const t = await getTranslations("Admin");
  return { title: t("createBusiness.title"), robots: { index: false } };
}

export default function NewBusinessPage() {
  return <NewBusiness />;
}
