import { getTranslations } from "next-intl/server";
import { Suspense } from "react";

import { Calendar } from "./calendar";

export async function generateMetadata() {
  const t = await getTranslations("Console.nav");
  return { title: t("calendar") };
}

export default function CalendarPage() {
  // useSearchParams needs a Suspense boundary to build statically; the shell has already loaded by then.
  return (
    <Suspense>
      <Calendar />
    </Suspense>
  );
}
