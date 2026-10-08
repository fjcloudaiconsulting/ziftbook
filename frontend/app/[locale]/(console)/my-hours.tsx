"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";

import { canEditHours, type Role } from "@/lib/console";
import { zoneCity } from "@/lib/week";

import { HoursSection } from "./hours-section";
import { PersonActions } from "./team/team";
import { BlockedTime, PersonTabs } from "./time-off-section";
import { useConsole } from "../_ui/console";
import { Heading } from "../_ui/parts";
import uiStyles from "../_ui/ui.module.css";

/** `/my-hours` (working hours) and `/my-hours/time-off` (blocked time): one shared heading and
 * lede, so switching tabs never makes the header jump. While the blocked-time tab is showing its
 * own form, this header hides so the form's own heading is the only `<h1>` on screen. */
export function MyHours({ tab }: { tab: "hours" | "timeOff" }) {
  const { session, settings, updateSession } = useConsole();
  const t = useTranslations("Console.hours");
  const nav = useTranslations("Console.nav");
  const person = useTranslations("Console.person");
  const city = zoneCity(settings.timezone);
  const editable = canEditHours(session.role as Role, true, settings.workers_edit_own_hours);
  const [inForm, setInForm] = useState(false);
  const name = session.display_name || person("nameNotSet");

  return (
    <>
      {!inForm && (
        <>
          <Heading focus>{nav("myHours")}</Heading>
          <p className={uiStyles.lede}>{t("myHoursLede", { city })}</p>
          {/* Your own name, so a team member can fix it too (the API allows it: may_manage). */}
          <PersonActions
            memberId={session.member_id}
            displayName={session.display_name}
            onNameSaved={(saved) => updateSession({ display_name: saved })}
          />
          <PersonTabs workingHoursHref="/my-hours" blockedTimeHref="/my-hours/time-off" active={tab} />
        </>
      )}
      {tab === "hours" ? (
        <HoursSection memberId={session.member_id} editable={editable} ownerView={false} />
      ) : (
        <BlockedTime memberId={session.member_id} personName={name} onFormMode={setInForm} />
      )}
    </>
  );
}
