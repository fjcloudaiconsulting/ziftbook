import { getTranslations } from "next-intl/server";

import { Screen } from "../../_ui/parts";
import { CompleteSignUp } from "./complete-sign-up";

export async function generateMetadata() {
  const t = await getTranslations("CompleteSignUp");
  return { title: t("title") };
}

export default function CompleteSignUpPage() {
  return (
    <Screen>
      <CompleteSignUp />
    </Screen>
  );
}
