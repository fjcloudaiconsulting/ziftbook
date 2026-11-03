import { getTranslations } from "next-intl/server";

import { Screen } from "../../_ui/parts";
import { ConfirmContactEmail } from "./confirm-contact-email";

export async function generateMetadata() {
  const t = await getTranslations("ContactEmail");
  return { title: t("title") };
}

export default function ConfirmContactEmailPage() {
  return (
    <Screen>
      <ConfirmContactEmail />
    </Screen>
  );
}
