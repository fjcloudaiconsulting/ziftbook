import { getTranslations } from "next-intl/server";

import { Screen } from "./_ui/parts";
import { Home } from "./home";

export async function generateMetadata() {
  const t = await getTranslations("Metadata");
  return { title: t("title") };
}

export default function HomePage() {
  return (
    <Screen>
      <Home />
    </Screen>
  );
}
