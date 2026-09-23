"use client";

import { useTranslations } from "next-intl";

import { zoneCity } from "@/lib/week";

import { HoursSection } from "./hours-section";
import { useConsole } from "../_ui/console";
import { Heading } from "../_ui/parts";
import uiStyles from "../_ui/ui.module.css";

export function MyHours() {
  const { session, settings } = useConsole();
  const t = useTranslations("Console.hours");
  const nav = useTranslations("Console.nav");
  const city = zoneCity(settings.timezone);

  return (
    <>
      <Heading focus>{nav("myHours")}</Heading>
      <p className={uiStyles.lede}>{t("myHoursLede", { city })}</p>
      <HoursSection memberId={session.member_id} editable={settings.workers_edit_own_hours} ownerView={false} />
    </>
  );
}
