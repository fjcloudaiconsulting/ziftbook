import { getTranslations } from "next-intl/server";

import { AdminHome } from "./admin-home";

export async function generateMetadata() {
  const t = await getTranslations("Admin");
  return { title: t("homeTitle"), robots: { index: false } };
}

export default function AdminPage() {
  return <AdminHome />;
}
