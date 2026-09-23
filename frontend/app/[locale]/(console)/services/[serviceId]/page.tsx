import { getTranslations } from "next-intl/server";

import { EditService } from "../services";

export async function generateMetadata() {
  const t = await getTranslations("Console.nav");
  return { title: t("services") };
}

export default async function EditServicePage({ params }: { params: Promise<{ serviceId: string }> }) {
  const { serviceId } = await params;
  return <EditService serviceId={serviceId} />;
}
