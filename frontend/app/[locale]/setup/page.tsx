import { notFound } from "next/navigation";
import { connection } from "next/server";
import { getTranslations } from "next-intl/server";

import { readNeedsSetup } from "@/lib/setup-gate";

import { Screen } from "../_ui/parts";
import { SetupForm } from "./setup-form";

export async function generateMetadata() {
  const t = await getTranslations("Setup");
  return { title: t("title"), robots: { index: false } };
}

export default async function SetupPage() {
  // Per request: without this the page is prerendered at build, when the API is unreachable and
  // the read below fails open, and then serves that static form forever.
  await connection();
  // Fails open: if the API can't be asked, the form shows and the backend decides on submit.
  if (!(await readNeedsSetup().catch(() => true))) notFound();
  return (
    <Screen>
      <SetupForm />
    </Screen>
  );
}
