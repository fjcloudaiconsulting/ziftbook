import { getTranslations } from "next-intl/server";

import { Screen } from "../_ui/parts";
import { ManageBooking } from "./manage-booking";

export async function generateMetadata() {
  const t = await getTranslations("BookingLink");
  return { title: t("title") };
}

export default function ManageBookingPage() {
  return (
    <Screen>
      <ManageBooking />
    </Screen>
  );
}
