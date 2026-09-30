"use client";

import { useTranslations } from "next-intl";
import { type MouseEvent, useState } from "react";

import type { MemberOut } from "@/api-client";
import { blockPrefill } from "@/lib/new-booking";

import { useConsole } from "../../_ui/console";
import uiStyles from "../../_ui/ui.module.css";
import { BlockedTimeForm } from "../time-off-section";
import { PanelFrame } from "./panel-frame";

/** Block time from the calendar: the time-off form, filled in from the empty spot, with a "Who" (an owner
 * picks anyone, a team member is themselves). */
export function BlockPanel({
  at,
  member,
  team,
  date,
  closeHref,
  onClose,
  onCancel,
  onDone,
}: {
  at: string | null;
  member: string | null;
  team: MemberOut[];
  date: string;
  closeHref: string;
  onClose(event: MouseEvent<HTMLAnchorElement>): void;
  onCancel(): void;
  onDone(): void;
}) {
  const { session, settings } = useConsole();
  const t = useTranslations("Console.calendar");
  const person = useTranslations("Console.person");
  const isOwner = session.role === "owner";
  const [who, setWho] = useState(member ?? session.member_id);
  const [prefill] = useState(() => (at ? blockPrefill(at, settings.timezone) : { firstDay: date, startTime: "09:00", endTime: "10:00" }));
  const nameOf = (m: MemberOut) => m.display_name ?? person("nameNotSet");
  const chosen = team.find((m) => m.member_id === who);
  const name = chosen ? nameOf(chosen) : (session.display_name ?? person("nameNotSet"));

  return (
    <PanelFrame id="panel-title" title={t("blockTitle")} closeHref={closeHref} onClose={onClose}>
      {isOwner && (
        <div className={uiStyles.field}>
          <label className={uiStyles.label} htmlFor="block-who">
            {t("blockWho")}
          </label>
          <div className={uiStyles.input}>
            <select id="block-who" value={who} onChange={(event) => setWho(event.target.value)}>
              {team.map((m) => (
                <option key={m.member_id} value={m.member_id}>
                  {nameOf(m)}
                </option>
              ))}
            </select>
          </div>
        </div>
      )}
      <BlockedTimeForm memberId={who} tz={settings.timezone} personName={name} editing={null} prefill={prefill} compact onDone={onDone} onCancel={onCancel} />
      <p className={uiStyles.hint}>{t("blockHint")}</p>
    </PanelFrame>
  );
}
