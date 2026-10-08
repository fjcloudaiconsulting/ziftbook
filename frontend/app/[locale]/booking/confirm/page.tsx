import { getTranslations } from "next-intl/server";

import { ConfirmBooking } from "./confirm-booking";

export async function generateMetadata() {
  const t = await getTranslations("BookingPage");
  return { title: t("confirmTitle") };
}

/** ZIF-117: where the emailed link lands. No language switcher: the link already carries the
 * language, and the token in the fragment is read once and taken out of the address bar. */
export default function ConfirmBookingPage() {
  return <ConfirmBooking />;
}
