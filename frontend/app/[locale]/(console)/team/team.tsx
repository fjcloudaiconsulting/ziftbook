"use client";

import { useLocale, useTranslations } from "next-intl";
import { type FormEvent, useEffect, useId, useRef, useState } from "react";

import {
  invitesCreate,
  invitesDelete,
  invitesList,
  type InviteOut,
  membersList,
  membersSetDisplayName,
  type MemberOut,
  workingHoursRead,
} from "@/api-client";
import { Link } from "@/i18n/navigation";
import { canEditHours, dateLocale, type Role, showSetName } from "@/lib/console";
import { canInvite, expiresIn, isRowExpired, upsertInvite } from "@/lib/team";
import { teamHoursSummary, zoneCity } from "@/lib/week";

import { HoursSection } from "../hours-section";
import { JSON_WRITE, SignedOutBanner, useConsole } from "../../_ui/console";
import styles from "../../_ui/console.module.css";
import { Banner, EmailField, FieldError, Heading, Mark, problem, Submit } from "../../_ui/parts";
import uiStyles from "../../_ui/ui.module.css";

/** A member's "Works {days}" (or "No working hours yet") row meta, from one working-hours GET.
 * `null` while that member's own read hasn't come back yet: the row shows nothing there rather
 * than a stale "No working hours yet". */
type HoursSummaries = Record<string, { weekday: number; starts_at: string }[] | null>;

function HoursMeta({ shifts, locale }: { shifts: { weekday: number; starts_at: string }[] | null; locale: string }) {
  const t = useTranslations("Console.team");
  const tWeek = useTranslations("Console.week");
  if (shifts === null) return null;
  const days = teamHoursSummary(shifts, dateLocale(locale), (from, to) => tWeek("dayRange", { from, to }));
  return <span className={styles.rowMeta}>{days === null ? t("noHours") : t("worksLabel", { days })}</span>;
}

/** Opened by the header's or the empty state's "Invite someone" button (undrawn, U8). Focus
 * lands on the email field on open, and the signed-out banner is the page's one copy (owned by
 * `TeamList`), not this form's own. */
function InviteForm({
  onCancel,
  onSent,
  onSignedOut,
}: {
  onCancel(): void;
  onSent(invite: InviteOut): void;
  onSignedOut(value: boolean): void;
}) {
  const t = useTranslations("Console.invites");
  const tConsole = useTranslations("Console");
  const form = useTranslations("Form");
  const { call } = useConsole();
  const [email, setEmail] = useState("");
  const [emailError, setEmailError] = useState<string>();
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const submitting = useRef(false);
  const emailInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    emailInput.current?.focus();
  }, []);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (submitting.current) return;
    submitting.current = true;
    setEmailError(undefined);
    setError(null);
    onSignedOut(false);
    setSending(true);
    let outcome;
    try {
      outcome = await call(() => invitesCreate({ body: { email } }), { write: true });
    } finally {
      setSending(false);
      submitting.current = false;
    }
    if (outcome.status === 201 && outcome.data) {
      onSent(outcome.data);
    } else if (outcome.status === 401) {
      onSignedOut(true);
    } else if (outcome.status === 422) {
      setEmailError(form("invalidEmail"));
    } else if (outcome.status === 409) {
      setError(t("alreadyMember"));
    } else if (outcome.status === 429) {
      setError(t("rateLimited"));
    } else {
      setError(`${form(problem(outcome))} ${tConsole("notSaved")}`);
    }
  }

  return (
    <form className={uiStyles.stack} noValidate onSubmit={onSubmit}>
      {error && <Banner tone="error">{error}</Banner>}
      <EmailField value={email} onChange={setEmail} error={emailError} autoComplete="off" inputRef={emailInput} />
      <div className={styles.dayActions}>
        <Submit busy={sending} busyLabel={form("sending")}>
          {t("sendInvite")}
        </Submit>
        <button className={uiStyles.textButton} type="button" disabled={sending} onClick={onCancel}>
          {t("cancel")}
        </button>
      </div>
    </form>
  );
}

/** A pending invite's row: email, day count or "Invite expired" (`InviteOut.expired`, or zero
 * days left by the client's own clock), Send again and Cancel (revoke). Both actions stay offered
 * on an expired row (U9). */
function InviteRow({
  invite,
  onResent,
  onRevoked,
  onRevokeStale,
  onSignedOut,
  sendAgainRef,
}: {
  invite: InviteOut;
  onResent(invite: InviteOut): void;
  onRevoked(id: string): void;
  /** 404: another tab already revoked it. The list is refetched rather than assumed correct. */
  onRevokeStale(): void;
  onSignedOut(value: boolean): void;
  sendAgainRef(id: string, el: HTMLButtonElement | null): void;
}) {
  const t = useTranslations("Console.invites");
  const tConsole = useTranslations("Console");
  const form = useTranslations("Form");
  const { call } = useConsole();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const busyRef = useRef(false);

  async function resend() {
    if (busyRef.current) return;
    busyRef.current = true;
    setError(null);
    onSignedOut(false);
    setBusy(true);
    let outcome;
    try {
      outcome = await call(() => invitesCreate({ body: { email: invite.email } }), { write: true });
    } finally {
      setBusy(false);
      busyRef.current = false;
    }
    if (outcome.status === 201 && outcome.data) {
      onResent(outcome.data);
    } else if (outcome.status === 401) {
      onSignedOut(true);
    } else if (outcome.status === 409) {
      setError(t("alreadyMember"));
    } else if (outcome.status === 429) {
      setError(t("rateLimited"));
    } else {
      setError(`${form(problem(outcome))} ${tConsole("notSaved")}`);
    }
  }

  async function revoke() {
    if (busyRef.current) return;
    busyRef.current = true;
    setError(null);
    onSignedOut(false);
    setBusy(true);
    let outcome;
    try {
      // json_only (main.py) refuses any non-GET request without a JSON content type, even a
      // bodyless DELETE: JSON_WRITE (console.tsx) forces the generated client to send one.
      outcome = await call(() => invitesDelete({ path: { invite_id: invite.id }, ...JSON_WRITE }), { write: true });
    } finally {
      setBusy(false);
      busyRef.current = false;
    }
    if (outcome.status === 204) {
      onRevoked(invite.id);
    } else if (outcome.status === 404) {
      onRevokeStale();
    } else if (outcome.status === 401) {
      onSignedOut(true);
    } else {
      setError(`${form(problem(outcome))} ${tConsole("notSaved")}`);
    }
  }

  return (
    <li>
      {error && <Banner tone="error">{error}</Banner>}
      <div className={styles.rowStatic}>
        <span className={styles.rowMain}>
          <span className={styles.rowTitle}>{invite.email}</span>
          <span className={styles.rowMeta}>
            {isRowExpired(invite, new Date()) ? t("expired") : t("expiresIn", { n: expiresIn(invite.expires_at, new Date()) })}
          </span>
        </span>
        <span className={styles.rowActions}>
          <button
            ref={(el) => sendAgainRef(invite.id, el)}
            className={`${uiStyles.button} ${uiStyles.secondary} ${uiStyles.small}`}
            type="button"
            disabled={busy}
            onClick={resend}
          >
            {t("sendAgain")}
          </button>
          <button
            className={`${uiStyles.button} ${uiStyles.secondary} ${uiStyles.small}`}
            type="button"
            disabled={busy}
            aria-label={t("cancelInvite", { email: invite.email })}
            onClick={revoke}
          >
            {t("cancel")}
          </button>
        </span>
      </div>
    </li>
  );
}

export function TeamList() {
  const { session, call } = useConsole();
  const locale = useLocale();
  const t = useTranslations("Console.team");
  const nav = useTranslations("Console.nav");
  const account = useTranslations("Console.account");
  const person = useTranslations("Console.person");
  const tInvites = useTranslations("Console.invites");
  const form = useTranslations("Form");

  const [members, setMembers] = useState<MemberOut[] | null>(null);
  const [hours, setHours] = useState<HoursSummaries>({});
  const [invites, setInvites] = useState<InviteOut[] | null>(null);
  // A worker never fetches invites at all, so nothing is ever "loading" for it: the lazy
  // initializer (not a synchronous setState reachable from the mount effect) starts it settled.
  const [invitesLoaded, setInvitesLoaded] = useState(() => !canInvite(session.role as Role));
  const [invitesFailure, setInvitesFailure] = useState<ReturnType<typeof problem> | null>(null);
  const [inviting, setInviting] = useState(false);
  const [signedOut, setSignedOut] = useState(false);
  const [failure, setFailure] = useState<ReturnType<typeof problem> | null>(null);

  // Where focus goes after a revoke removes a row: the next row (or the previous one, if the
  // removed row was last), else the "Invite someone" button once the list is empty again.
  const sendAgainButtons = useRef<Map<string, HTMLButtonElement>>(new Map());
  const inviteButton = useRef<HTMLButtonElement>(null);
  const pendingFocusIndex = useRef<number | null>(null);
  // The header button unmounts while the form is open, so it isn't there yet the instant the form
  // closes: this fires once the next render has it back, whichever of the three things asked for it.
  const focusInviteButtonPending = useRef(false);

  function loadInvites() {
    if (!canInvite(session.role as Role)) return;
    call(() => invitesList()).then((outcome) => {
      if (outcome.status === 200 && outcome.data) {
        setInvites(outcome.data);
        setInvitesFailure(null);
      } else {
        setInvitesFailure(problem(outcome));
      }
      setInvitesLoaded(true);
    });
  }

  function applyInvite(invite: InviteOut) {
    setInvites((prev) => upsertInvite(prev ?? [], invite));
  }

  function revokeRow(id: string) {
    setInvites((prev) => {
      const list = prev ?? [];
      pendingFocusIndex.current = list.findIndex((i) => i.id === id);
      return list.filter((i) => i.id !== id);
    });
  }

  /** After Send invite, form Cancel, or a stale (404) revoke: the form or the row is gone, so
   * focus returns to the one control still on the page that opens it again. */
  function closeInviteForm() {
    focusInviteButtonPending.current = true;
    setInviting(false);
  }

  function reloadAfterStaleRevoke() {
    focusInviteButtonPending.current = true;
    loadInvites();
  }

  useEffect(() => {
    if (pendingFocusIndex.current === null) return;
    const idx = pendingFocusIndex.current;
    pendingFocusIndex.current = null;
    const list = invites ?? [];
    const neighbour = list[idx] ?? list[idx - 1];
    if (neighbour) sendAgainButtons.current.get(neighbour.id)?.focus();
    else inviteButton.current?.focus();
  }, [invites]);

  useEffect(() => {
    if (!focusInviteButtonPending.current) return;
    focusInviteButtonPending.current = false;
    inviteButton.current?.focus();
  }, [inviting, invites, invitesFailure]);

  function load() {
    call(() => membersList()).then(async (outcome) => {
      if (outcome.status !== 200 || !outcome.data) {
        setFailure(problem(outcome));
        return;
      }
      setFailure(null);
      setMembers(outcome.data);
      // The self row never shows an hours summary, so it's never fetched.
      // ponytail: one GET per other member, run in parallel; fine up to tens of members, revisit
      // if a business ever grows past that.
      const others = outcome.data.filter((m) => m.member_id !== session.member_id);
      const entries = await Promise.all(
        others.map(async (m) => {
          const r = await call(() => workingHoursRead({ path: { member_id: m.member_id } }));
          return [m.member_id, r.status === 200 && r.data ? r.data : null] as const;
        }),
      );
      setHours(Object.fromEntries(entries));
    });
    loadInvites();
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
  const mayInvite = canInvite(session.role as Role);
  // While invites are loading (or failed to load), "alone" stays false: the owner never sees the
  // empty state flash before a pending invite it didn't know about yet.
  const invitesReady = invitesLoaded && !invitesFailure;
  const alone = invitesReady && others.length === 0 && (invites?.length ?? 0) === 0;

  return (
    <>
      <div className={styles.screenHead}>
        <Heading focus>{nav("team")}</Heading>
        {mayInvite && !alone && !inviting && (
          <button
            ref={inviteButton}
            className={`${uiStyles.button} ${uiStyles.primary} ${uiStyles.small}`}
            type="button"
            onClick={() => setInviting(true)}
          >
            {tInvites("inviteSomeone")}
          </button>
        )}
      </div>
      {signedOut && <SignedOutBanner />}
      {invitesFailure && (
        <>
          <Banner tone="error">{form(invitesFailure)}</Banner>
          <button className={uiStyles.textButton} type="button" onClick={loadInvites}>
            {form("tryAgain")}
          </button>
        </>
      )}
      {inviting && (
        <InviteForm
          onCancel={closeInviteForm}
          onSent={(invite) => {
            applyInvite(invite);
            closeInviteForm();
          }}
          onSignedOut={setSignedOut}
        />
      )}
      <ul className={styles.list}>
        {self && (
          <li>
            <Link href={`/team/${self.member_id}`} className={styles.rowLink}>
              <span className={styles.rowMain}>
                <span className={self.display_name ? styles.rowTitle : `${styles.rowTitle} ${styles.unset}`}>
                  {self.display_name || person("nameNotSet")}
                </span>
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
      {(invites?.length ?? 0) > 0 && (
        <>
          <h2 className={styles.heading2}>{tInvites("invited")}</h2>
          <ul className={styles.list}>
            {invites!.map((invite) => (
              <InviteRow
                key={invite.id}
                invite={invite}
                onResent={applyInvite}
                onRevoked={revokeRow}
                onRevokeStale={reloadAfterStaleRevoke}
                onSignedOut={setSignedOut}
                sendAgainRef={(id, el) => {
                  if (el) sendAgainButtons.current.set(id, el);
                  else sendAgainButtons.current.delete(id);
                }}
              />
            ))}
          </ul>
          <p className={uiStyles.hint}>{tInvites("invitedHint")}</p>
        </>
      )}
      {alone && (
        <>
          <div className={uiStyles.empty}>
            <Mark icon="person" />
            <strong>{t("onlyYouTitle")}</strong>
            <span>{t("onlyYouBody")}</span>
            {mayInvite && !inviting && (
              <button ref={inviteButton} className={`${uiStyles.button} ${uiStyles.primary}`} type="button" onClick={() => setInviting(true)}>
                {tInvites("inviteSomeone")}
              </button>
            )}
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
  const [signedOut, setSignedOut] = useState(false);
  const [fieldError, setFieldError] = useState<string | null>(null);
  const errorId = `${id}-error`;
  // A state flag lags a tick behind synchronous re-entrant calls (three requestSubmit()s in one
  // event all read the same stale `saving` before any re-render), so the actual guard is this ref.
  const submitting = useRef(false);

  if (!open) {
    return (
      <button className={uiStyles.textButton} type="button" onClick={() => setOpen(true)}>
        {t("setName")}
      </button>
    );
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (submitting.current) return;
    const trimmed = value.trim();
    if (!trimmed) {
      setFieldError(t("nameRequired"));
      return;
    }
    submitting.current = true;
    setFieldError(null);
    setSignedOut(false);
    setError(null);
    setSaving(true);
    const outcome = await call(
      () => membersSetDisplayName({ path: { member_id: memberId }, body: { display_name: trimmed } }),
      { write: true },
    );
    setSaving(false);
    submitting.current = false;
    if (outcome.status === 200 && outcome.data) {
      onSaved(outcome.data.display_name ?? trimmed);
      setOpen(false);
    } else if (outcome.status === 401) {
      setSignedOut(true);
    } else {
      setError(form(problem(outcome)));
    }
  }

  return (
    <form className={uiStyles.stack} noValidate onSubmit={onSubmit}>
      {signedOut && <SignedOutBanner />}
      {error && <Banner tone="error">{error}</Banner>}
      <div className={uiStyles.field}>
        <label className={uiStyles.label} htmlFor={id}>
          {t("nameLabel")}
        </label>
        <div className={uiStyles.input}>
          <input
            id={id}
            type="text"
            maxLength={60}
            value={value}
            disabled={saving}
            aria-invalid={fieldError ? true : undefined}
            aria-describedby={fieldError ? errorId : undefined}
            onChange={(e) => setValue(e.target.value)}
          />
        </div>
        {fieldError && <FieldError id={errorId}>{fieldError}</FieldError>}
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
      {showSetName(displayName) && (
        <SetNameField
          memberId={memberId}
          onSaved={(name) => {
            setDisplayName(name);
            if (memberId === session.member_id) updateSession({ display_name: name });
          }}
        />
      )}
      <HoursSection
        memberId={memberId}
        editable={canEditHours(session.role as Role, memberId === session.member_id, settings.workers_edit_own_hours)}
        ownerView
      />
    </>
  );
}
