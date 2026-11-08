import { getTranslations } from "next-intl/server";
import { Suspense } from "react";

import { Settings } from "./settings";

export async function generateMetadata() {
  const t = await getTranslations("Console.nav");
  return { title: t("settings") };
}

export default function SettingsPage() {
  // useSearchParams (the tab) needs a Suspense boundary to build statically; the shell has already loaded by then.
  return (
    <Suspense>
      <Settings />
    </Suspense>
  );
}
