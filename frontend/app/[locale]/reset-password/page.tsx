import { getTranslations } from "next-intl/server";

import { Screen } from "../_ui/parts";
import { ResetPassword } from "./reset-password";

export async function generateMetadata() {
  const t = await getTranslations("ResetPassword");
  return { title: t("title") };
}

export default function ResetPasswordPage() {
  return (
    <Screen>
      <ResetPassword />
    </Screen>
  );
}
