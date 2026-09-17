import { getTranslations } from "next-intl/server";

import { Screen } from "../_ui/parts";
import { AcceptInvite } from "./accept-invite";

export async function generateMetadata() {
  const t = await getTranslations("Invite");
  return { title: t("title") };
}

export default function AcceptInvitePage() {
  return (
    <Screen>
      <AcceptInvite />
    </Screen>
  );
}
