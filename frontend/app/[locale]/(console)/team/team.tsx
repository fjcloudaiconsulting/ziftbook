"use client";

import { useLocale, useTranslations } from "next-intl";
import { type FormEvent, useEffect, useId, useState } from "react";

import { membersList, membersSetDisplayName, type MemberOut, workingHoursRead } from "@/api-client";
import { Link } from "@/i18n/navigation";
import { dateLocale } from "@/lib/console";
import { daysSummary, zoneCity } from "@/lib/week";

import { HoursSection } from "../hours-section";
import { useConsole } from "../../_ui/console";
import styles from "../../_ui/console.module.css";
import { Banner, Heading, Mark, problem, Submit } from "../../_ui/parts";
import uiStyles from "../../_ui/ui.module.css";

/** A member's "Works {days}" (or "No working hours yet") row meta, from one working-hours GET.
 * `null` while that member's own read hasn't come back yet: the row shows nothing there rather
 * than a stale "No working hours yet". */
type HoursSummaries = Record<string, { weekday: number; starts_at: string }[] | null>;

function HoursMeta({ shifts, locale }: { shifts: { weekday: number; starts_at: string }[] | null; locale: string }) {
  const t = useTranslations("Console.team");
  const tWeek = useTranslations("Console.week");
  if (shifts === null) return null;
  if (shifts.length === 0) return <span className={styles.rowMeta}>{t("noHours")}</span>;
  const weekdays = [...new Set(shifts.map((s) => s.weekday))];
  const days = daysSummary(weekdays, dateLocale(locale), (from, to) => tWeek("dayRange", { from, to }));
  return <span className={styles.rowMeta}>{t("worksLabel", { days })}</span>;
}

export function TeamList() {
  const { session, call } = useConsole();
  const locale = useLocale();
  const t = useTranslations("Console.team");
  const nav = useTranslations("Console.nav");
  const account = useTranslations("Console.account");
  const person = useTranslations("Console.person");
  const form = useTranslations("Form");

  const [members, setMembers] = useState<MemberOut[] | null>(null);
  const [hours, setHours] = useState<HoursSummaries>({});
  const [failure, setFailure] = useState<ReturnType<typeof problem> | null>(null);

  function load() {
    call(() => membersList()).then(async (outcome) => {
      if (outcome.status !== 200 || !outcome.data) {
        setFailure(problem(outcome));
        return;
      }
      setFailure(null);
      setMembers(outcome.data);
      // ponytail: one GET per member, run in parallel; fine up to tens of members, revisit if a
      // business ever grows past that.
      const entries = await Promise.all(
        outcome.data.map(async (m) => {
          const r = await call(() => workingHoursRead({ path: { member_id: m.member_id } }));
          return [m.member_id, r.status === 200 && r.data ? r.data : null] as const;
        }),
      );
      setHours(Object.fromEntries(entries));
    });
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (failure) {
    return (
      <>
        <Heading focus>{nav("team")}</Heading>
        <Banner tone="error">{form(failure)}</Banner>
        <button className={uiStyles.textButton} type="button" onClick={load}>
          {form("tryAgain")}
        </button>
      </>
    );
  }

  if (!members) return <Heading focus>{nav("team")}</Heading>;

  const self = members.find((m) => m.member_id === session.member_id);
  const others = members.filter((m) => m.member_id !== session.member_id);

  return (
    <>
      <Heading focus>{nav("team")}</Heading>
      <ul className={styles.list}>
        {self && (
          <li>
            <Link href={`/team/${self.member_id}`} className={styles.rowLink}>
              <span className={styles.rowMain}>
                <span className={styles.rowTitle}>{self.display_name || person("nameNotSet")}</span>
                <span className={styles.rowMeta}>{t("selfMeta")}</span>
                <span className={styles.rowMeta}>{self.email}</span>
              </span>
              <span className={styles.rowEnd}>
                <span className={styles.pill}>{account(self.role === "owner" ? "owner" : "worker")}</span>
              </span>
              <Chevron />
            </Link>
          </li>
        )}
        {others.map((m) => (
          <li key={m.member_id}>
            <Link href={`/team/${m.member_id}`} className={styles.rowLink}>
              <span className={styles.rowMain}>
                <span className={m.display_name ? styles.rowTitle : `${styles.rowTitle} ${styles.unset}`}>
                  {m.display_name || person("nameNotSet")}
                </span>
                <span className={styles.rowMeta}>{m.email}</span>
                <HoursMeta shifts={hours[m.member_id] ?? null} locale={locale} />
                {!m.display_name && (
                  <span className={`${styles.pill} ${styles.pillWarn}`}>
                    <svg aria-hidden="true" viewBox="0 0 16 16" width="12" height="12">
                      <circle cx="8" cy="8" r="6.5" fill="none" stroke="currentColor" strokeWidth="1.6" />
                      <path d="M8 4.5v4M8 11h.01" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
                    </svg>
                    {t("unnamedWarning")}
                  </span>
                )}
              </span>
              <span className={styles.rowEnd}>
                <span className={styles.pill}>{account(m.role === "owner" ? "owner" : "worker")}</span>
              </span>
              <Chevron />
            </Link>
          </li>
        ))}
      </ul>
      {others.length === 0 && (
        <>
          <div className={uiStyles.empty}>
            <Mark icon="person" />
            <strong>{t("onlyYouTitle")}</strong>
            <span>{t("onlyYouBody")}</span>
          </div>
          <p className={uiStyles.hint}>{t("onlyYouHint")}</p>
        </>
      )}
    </>
  );
}

function Chevron() {
  return (
    <svg className={styles.chev} aria-hidden="true" viewBox="0 0 12 12" width="12" height="12">
      <path d="M4.5 2.5l3 3.5-3 3.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function BackLink() {
  const nav = useTranslations("Console.nav");
  return (
    <Link href="/team" className={styles.back}>
      <svg aria-hidden="true" viewBox="0 0 12 12" width="12" height="12">
        <path d="M7.5 2.5l-3 3.5 3 3.5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      {nav("team")}
    </Link>
  );
}

function SetNameField({ memberId, onSaved }: { memberId: string; onSaved(name: string): void }) {
  const t = useTranslations("Console.person");
  const form = useTranslations("Form");
  const { call } = useConsole();
  const id = useId();
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!open) {
    return (
      <button className={uiStyles.textButton} type="button" onClick={() => setOpen(true)}>
        {t("setName")}
      </button>
    );
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    const trimmed = value.trim();
    if (!trimmed) return;
    setSaving(true);
    const outcome = await call(
      () => membersSetDisplayName({ path: { member_id: memberId }, body: { display_name: trimmed } }),
      { write: true },
    );
    setSaving(false);
    if (outcome.status === 200 && outcome.data) {
      onSaved(outcome.data.display_name ?? trimmed);
      setOpen(false);
    } else if (outcome.status !== 401) {
      setError(form(problem(outcome)));
    }
  }

  return (
    <form className={uiStyles.stack} noValidate onSubmit={onSubmit}>
      {error && <Banner tone="error">{error}</Banner>}
      <div className={uiStyles.field}>
        <label className={uiStyles.label} htmlFor={id}>
          {t("nameLabel")}
        </label>
        <div className={uiStyles.input}>
          <input id={id} type="text" maxLength={60} value={value} disabled={saving} onChange={(e) => setValue(e.target.value)} />
        </div>
      </div>
      <div className={styles.dayActions}>
        <Submit busy={saving} busyLabel={form("sending")}>
          {t("save")}
        </Submit>
        <button className={uiStyles.textButton} type="button" disabled={saving} onClick={() => setOpen(false)}>
          {t("cancel")}
        </button>
      </div>
    </form>
  );
}

export function Person({ memberId }: { memberId: string }) {
  const { session, settings, call, updateSession } = useConsole();
  const t = useTranslations("Console.person");
  const account = useTranslations("Console.account");
  const form = useTranslations("Form");
  const city = zoneCity(settings.timezone);

  const [members, setMembers] = useState<MemberOut[] | null>(null);
  const [displayName, setDisplayName] = useState<string | null>(null);
  const [failure, setFailure] = useState<ReturnType<typeof problem> | null>(null);

  function load() {
    call(() => membersList()).then((outcome) => {
      if (outcome.status === 200 && outcome.data) {
        setFailure(null);
        setMembers(outcome.data);
        const found = outcome.data.find((m) => m.member_id === memberId);
        setDisplayName(found?.display_name ?? null);
      } else {
        setFailure(problem(outcome));
      }
    });
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [memberId]);

  if (failure) {
    return (
      <>
        <BackLink />
        <Banner tone="error">{form(failure)}</Banner>
        <button className={uiStyles.textButton} type="button" onClick={load}>
          {form("tryAgain")}
        </button>
      </>
    );
  }

  if (!members) return <BackLink />;

  const member = members.find((m) => m.member_id === memberId);
  if (!member) {
    return (
      <>
        <BackLink />
        <Banner tone="error">{t("notFound")}</Banner>
      </>
    );
  }

  return (
    <>
      <BackLink />
      <Heading focus>
        {displayName ? displayName : <span className={styles.unset}>{t("nameNotSet")}</span>}
      </Heading>
      <p className={uiStyles.lede}>{t("lede", { email: member.email, role: account(member.role === "owner" ? "owner" : "worker"), city })}</p>
      {!displayName && (
        <SetNameField
          memberId={memberId}
          onSaved={(name) => {
            setDisplayName(name);
            if (memberId === session.member_id) updateSession({ display_name: name });
          }}
        />
      )}
      <HoursSection memberId={memberId} editable ownerView />
    </>
  );
}
