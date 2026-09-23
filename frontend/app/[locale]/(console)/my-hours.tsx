"use client";

import { useTranslations } from "next-intl";

import { canEditHours, type Role } from "@/lib/console";
import { zoneCity } from "@/lib/week";

import { HoursSection } from "./hours-section";
import { BlockedTime, PersonTabs } from "./time-off-section";
import { useConsole } from "../_ui/console";
import { Heading } from "../_ui/parts";
import uiStyles from "../_ui/ui.module.css";

export function MyHours() {
  const { session, settings } = useConsole();
  const t = useTranslations("Console.hours");
  const nav = useTranslations("Console.nav");
  const city = zoneCity(settings.timezone);
  const editable = canEditHours(session.role as Role, true, settings.workers_edit_own_hours);

  return (
    <>
      <Heading focus>{nav("myHours")}</Heading>
      <p className={uiStyles.lede}>{t("myHoursLede", { city })}</p>
      <PersonTabs workingHoursHref="/my-hours" blockedTimeHref="/my-hours/time-off" active="hours" />
      <HoursSection memberId={session.member_id} editable={editable} ownerView={false} />
    </>
  );
}

/** `/my-hours/time-off` (worker): the same heading as `MyHours`, with the blocked-time tab active. */
export function MyHoursTimeOff() {
  const { session } = useConsole();
  const nav = useTranslations("Console.nav");

  return (
    <>
      <Heading focus>{nav("myHours")}</Heading>
      <PersonTabs workingHoursHref="/my-hours" blockedTimeHref="/my-hours/time-off" active="timeOff" />
      <BlockedTime memberId={session.member_id} />
    </>
  );
}
