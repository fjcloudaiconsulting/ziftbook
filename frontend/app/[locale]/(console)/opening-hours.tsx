"use client";

import { useLocale, useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import { openingHoursRead, openingHoursReplace } from "@/api-client";
import { Link } from "@/i18n/navigation";
import { daysFromShifts, type Day, emptyWeek, zoneCity } from "@/lib/week";

import { useConsole } from "../_ui/console";
import { WeekEditor } from "../_ui/week-editor";
import { Banner, Heading, Mark, problem } from "../_ui/parts";
import uiStyles from "../_ui/ui.module.css";

export function OpeningHours() {
  const { call, settings, session } = useConsole();
  const locale = useLocale();
  const t = useTranslations("Console.opening");
  const tWeek = useTranslations("Console.week");
  const nav = useTranslations("Console.nav");
  const form = useTranslations("Form");

  const [days, setDays] = useState<Day[] | null>(null);
  const [rawEmpty, setRawEmpty] = useState(false);
  const [showEditor, setShowEditor] = useState(false);
  const [failure, setFailure] = useState<ReturnType<typeof problem> | null>(null);

  function load() {
    call(() => openingHoursRead()).then((outcome) => {
      if (outcome.status === 200 && outcome.data) {
        const shifts = outcome.data;
        setFailure(null);
        setRawEmpty(shifts.length === 0);
        setDays(shifts.length === 0 ? emptyWeek() : daysFromShifts(shifts));
      } else {
        setFailure(problem(outcome));
      }
    });
  }

  useEffect(() => {
    load();
    // Runs once, on mount: the shell already re-reads the session and settings before every write.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const city = zoneCity(settings.timezone);

  if (failure) {
    return (
      <>
        <Heading focus>{nav("openingHours")}</Heading>
        <Banner tone="error">{form(failure)}</Banner>
        <button className={uiStyles.textButton} type="button" onClick={load}>
          {form("tryAgain")}
        </button>
      </>
    );
  }

  if (!days) return <Heading focus>{nav("openingHours")}</Heading>;

  if (rawEmpty && !showEditor) {
    return (
      <>
        <Heading focus>{nav("openingHours")}</Heading>
        <p className={uiStyles.lede}>{t("emptyLede", { business: session.business_name, city })}</p>
        <div className={uiStyles.empty}>
          <Mark icon="calendar" />
          <strong>{t("empty.title")}</strong>
          <span>{t("empty.body")}</span>
          <button className={`${uiStyles.button} ${uiStyles.primary}`} type="button" onClick={() => setShowEditor(true)}>
            {t("empty.cta")}
          </button>
        </div>
        <p className={uiStyles.hint}>{t("empty.hint")}</p>
      </>
    );
  }

  return (
    <>
      <Heading focus>{nav("openingHours")}</Heading>
      <p className={uiStyles.lede}>{t("editorLede", { city })}</p>
      <Banner tone="info">
        {t.rich("note", {
          link: (chunks) => (
            <Link className={uiStyles.textButton} href="/team">
              {chunks}
            </Link>
          ),
        })}
      </Banner>
      <WeekEditor
        initial={days}
        envelope={null}
        locale={locale}
        t={t}
        tWeek={tWeek}
        savedMessage={t("saved")}
        onSave={(body) => call(() => openingHoursReplace({ body }), { write: true })}
      />
    </>
  );
}
