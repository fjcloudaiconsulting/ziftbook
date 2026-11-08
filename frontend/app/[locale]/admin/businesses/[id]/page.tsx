import { getTranslations } from "next-intl/server";

import { Business } from "./business";

// A fixed title: the business's name would need the admin's session server-side, and would land in browser history.
export async function generateMetadata() {
  const t = await getTranslations("Admin");
  return { title: t("businessTitle"), robots: { index: false } };
}

export default async function BusinessPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <Business id={id} />;
}
