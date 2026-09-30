"use client";

import { useLocale, useTranslations } from "next-intl";
import { type FormEvent, type MouseEvent, type ReactNode, useEffect, useRef, useState } from "react";

import {
  availabilityMerchantRead,
  type BookingDetailOut,
  bookingApprovalsCreate,
  bookingApprovalsRead,
  bookingApprovalsReschedule,
  type ClientOut,
  clientsList,
  type MemberOut,
  type ServiceOut,
  servicesList,
  type WorkerOut,
} from "@/api-client";
import { Link } from "@/i18n/navigation";
import { dayWindow } from "@/lib/calendar";
import { dateLocale } from "@/lib/console";
import { formatMoney } from "@/lib/money";
import {
  bookingBody,
  defaultService,
  groupSlots,
  initialPick,
  isLatest,
  isUnchanged,
  moveBody,
  type Pick,
  pickInstant,
  searchTerm,
  shiftStrip,
  stripStart,
  submitFailure,
} from "@/lib/new-booking";
import { type Locale, type NameMap, serviceName } from "@/lib/services";
import { addDaysISO, localDateISO, localTime } from "@/lib/time-off";

import { SignedOutBanner, useConsole } from "../../_ui/console";
import { Banner, FieldError, problem } from "../../_ui/parts";
import uiStyles from "../../_ui/ui.module.css";
import { LoadFailure, Skeleton } from "../today";
import css from "./calendar.module.css";
import { PanelFrame } from "./panel-frame";

export type Done = { id: string; startsAt: string; message: string };

type Props = {
  mode: "new" | "move";
  /** The booking being moved. */
  bookingId: string | null;
  prefill: { at: string | null; with: string | null };
  team: MemberOut[];
  todayISO: string;
  /** The calendar's own date: where the day strip starts when nothing else says. */
  date: string;
  closeHref: string;
  onClose(event: MouseEvent<HTMLAnchorElement>): void;
  onDone(done: Done): void;
};

/** A new booking for a phone or walk-in client, or a move of one already there: the same day and time
 * pickers, "Pick another time" included. Loads what it needs (the services, or the booking) and then
 * hands over to `Fields`, whose state starts from it. */
export function BookingForm(props: Props) {
  const { call } = useConsole();
  const t = useTranslations("Console.calendar");
  const { mode, bookingId } = props;
  const [services, setServices] = useState<ServiceOut[] | null>(null);
  const [booking, setBooking] = useState<BookingDetailOut | null>(null);
  const [failure, setFailure] = useState<{ status: number; problem: ReturnType<typeof problem> } | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    const request = mode === "new" ? call(() => servicesList()) : call(() => bookingApprovalsRead({ path: { booking_id: bookingId! } }));
    request.then((outcome) => {
      if (outcome.status !== 200 || !outcome.data) return setFailure({ status: outcome.status, problem: problem(outcome) });
      setFailure(null);
      if (mode === "new") setServices(outcome.data as ServiceOut[]);
      else setBooking(outcome.data as BookingDetailOut);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, bookingId, reload]);

  const frame = (body: ReactNode) => (
    <PanelFrame id="panel-title" title={mode === "new" ? t("newTitle") : t("moveTitle")} closeHref={props.closeHref} onClose={props.onClose}>
      {body}
    </PanelFrame>
  );
  if (failure) {
    return frame(
      failure.status === 403 || failure.status === 404 ? (
        <Banner tone="error">{failure.status === 403 ? t("detailForbidden") : t("detailGone")}</Banner>
      ) : (
        <LoadFailure failure={failure.problem} onRetry={() => setReload((n) => n + 1)} />
      ),
    );
  }
  if (mode === "new" && services) {
    const live = services.filter((s) => !s.archived);
    if (live.length === 0) {
      return frame(
        <>
          <p>{t("noServices")}</p>
          <Link className={`${uiStyles.button} ${uiStyles.secondary} ${uiStyles.small}`} href="/services">
            {t("goServices")}
          </Link>
        </>,
      );
    }
    return frame(<Fields {...props} services={live} booking={null} />);
  }
  if (mode === "move" && booking) return frame(<Fields {...props} services={[]} booking={booking} />);
  return frame(<Skeleton />);
}

function Fields({ prefill, team, todayISO, date, onDone, services, booking }: Props & { services: ServiceOut[]; booking: BookingDetailOut | null }) {
  const { session, settings, call } = useConsole();
  const t = useTranslations("Console.calendar");
  const person = useTranslations("Console.person");
  const errorsT = useTranslations("Console.errors");
  const form = useTranslations("Form");
  const locale = useLocale();
  const tz = settings.timezone;
  const isOwner = session.role === "owner";
  const moving = booking !== null;
  const startAt = booking ? booking.starts_at : prefill.at;
  const uiLocale = dateLocale(locale);
  const ownService = (name: { [key: string]: string }) => serviceName(name as NameMap, locale as Locale, settings.language as Locale);

  // --- client (new only) ---
  const [client, setClient] = useState<ClientOut | null>(null);
  const [creating, setCreating] = useState(false);
  const [q, setQ] = useState("");
  const [results, setResults] = useState<{ term: string; list: ClientOut[] } | null>(null);
  const [newName, setNewName] = useState("");
  const [newPhone, setNewPhone] = useState("");
  const [newEmail, setNewEmail] = useState("");
  const searchSeq = useRef(0);

  // --- what, who, when ---
  const initialWith = moving ? booking.worker_id : isOwner ? prefill.with : session.member_id;
  const [serviceId, setServiceId] = useState(() => (moving ? booking.service_id : (defaultService(services, initialWith)?.id ?? "")));
  const [withId, setWithId] = useState(initialWith ?? "");
  const [anchor, setAnchor] = useState(() => stripStart(startAt ? localDateISO(new Date(startAt), tz) : date, todayISO));
  const [day, setDay] = useState<string | null>(startAt ? localDateISO(new Date(startAt), tz) : null);
  const [pick, setPick] = useState<Pick>({ kind: "none" });
  const [slots, setSlots] = useState<string[]>([]);
  const [workers, setWorkers] = useState<WorkerOut[]>(() => {
    const service = services.find((s) => s.id === serviceId);
    return team.filter((m) => !service || service.worker_ids.includes(m.member_id)).map((m) => ({ id: m.member_id, display_name: m.display_name }));
  });
  const [loadedKey, setLoadedKey] = useState("");
  const [availabilityFailure, setAvailabilityFailure] = useState<{ key: string; problem: ReturnType<typeof problem> } | null>(null);
  const [refetch, setRefetch] = useState(0);
  const availSeq = useRef(0);
  const initialised = useRef(false);

  // --- submit ---
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);
  const [emailError, setEmailError] = useState<string | null>(null);
  const [signedOut, setSignedOut] = useState(false);
  const submitting = useRef(false);
  const focusTarget = useRef<string | null>(null);

  useEffect(() => {
    const target = focusTarget.current;
    if (!target) return;
    focusTarget.current = null;
    document.getElementById(target)?.focus();
  });

  // Search as you type: each change makes every older answer stale, and a slow one never wins.
  useEffect(() => {
    const mine = ++searchSeq.current;
    const term = searchTerm(q);
    if (!term) return;
    const timer = setTimeout(() => {
      call(() => clientsList({ query: { q: term, limit: 8 } })).then((outcome) => {
        if (!isLatest(mine, searchSeq.current)) return;
        setResults({ term, list: outcome.status === 200 && outcome.data ? outcome.data : [] });
      });
    }, 250);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q]);

  // The free starts of the strip's week, for this service and person (a move sees the booking's own slot free).
  const availabilityKey = `${serviceId}|${withId}|${anchor}|${refetch}`;
  useEffect(() => {
    if (!serviceId || (moving && !withId)) return;
    const mine = ++availSeq.current;
    const range = dayWindow(anchor, tz, 7);
    call(() =>
      availabilityMerchantRead({
        path: { service_id: serviceId },
        query: { from: range.from, to: range.to, ...(withId && { member_id: withId }), ...(booking && { exclude: booking.id }) },
      }),
    ).then((outcome) => {
      if (!isLatest(mine, availSeq.current)) return;
      if (outcome.status !== 200 || !outcome.data) return setAvailabilityFailure({ key: availabilityKey, problem: problem(outcome) });
      setAvailabilityFailure(null);
      setSlots(outcome.data.slots);
      setWorkers(outcome.data.workers);
      if (!initialised.current) {
        initialised.current = true;
        const first = initialPick(startAt, outcome.data.slots, tz);
        setPick(first);
        if (first.kind === "slot") setDay(localDateISO(new Date(first.slot), tz));
      }
      setLoadedKey(availabilityKey);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [availabilityKey]);

  const dateFormat = new Intl.DateTimeFormat(uiLocale, { weekday: "short", day: "numeric", month: "short", timeZone: tz });
  const longDay = new Intl.DateTimeFormat(uiLocale, { weekday: "long", day: "numeric", month: "long", timeZone: "UTC" });
  const stripLabel = new Intl.DateTimeFormat(uiLocale, { weekday: "short", day: "numeric", timeZone: "UTC" });
  const asDate = (iso: string) => new Date(`${iso}T12:00:00Z`);
  const isToday = (instant: string) => localDateISO(new Date(instant), tz) === todayISO;
  const whenOf = (instant: string) => {
    const time = localTime(instant, tz);
    return isToday(instant) ? t("pickedToday", { time }) : t("pickedDate", { date: dateFormat.format(new Date(instant)), time });
  };

  const grouped = groupSlots(slots, tz);
  const strip = Array.from({ length: 7 }, (_, i) => addDaysISO(anchor, i));
  const selectedDay = day ?? strip.find((d) => grouped[d]) ?? strip[0];
  const days = strip.filter((d) => grouped[d] || d === selectedDay);
  const times = grouped[selectedDay] ?? [];
  const loading = Boolean(serviceId) && loadedKey !== availabilityKey && !(availabilityFailure && availabilityFailure.key === availabilityKey);
  const failedAvailability = availabilityFailure && availabilityFailure.key === availabilityKey ? availabilityFailure : null;

  const service = services.find((s) => s.id === serviceId);
  const withName = (id: string) =>
    workers.find((w) => w.id === id)?.display_name ?? team.find((m) => m.member_id === id)?.display_name ?? (id === session.member_id ? session.display_name : null) ?? person("nameNotSet");
  const prefillMember = prefill.with ? withName(prefill.with) : "";
  const clientName = moving ? booking.client_name : (client?.name ?? newName.trim());
  const clientEmail = moving ? booking.client_email : client ? client.email : newEmail.trim();
  const instant = pickInstant(pick, tz);
  const needsPerson = pick.kind === "other" && !withId;
  const clientReady = moving || client !== null || (creating && newName.trim() !== "");
  const complete = Boolean(serviceId) && clientReady && instant !== null && !needsPerson && !(moving && isUnchanged(instant, withId, booking));
  const serviceLabel = moving ? ownService(booking.service_name) : service ? ownService(service.name) : "";

  function choose(next: Pick) {
    setPick(next);
    setBanner(null);
  }
  function clearTime() {
    if (pick.kind === "slot") setPick({ kind: "none" });
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (submitting.current || !complete || !instant) return;
    submitting.current = true;
    setBanner(null);
    setEmailError(null);
    setSignedOut(false);
    setBusy(true);
    const override = pick.kind === "other";
    try {
      const outcome = moving
        ? await call(() => bookingApprovalsReschedule({ path: { booking_id: booking.id }, body: moveBody({ startsAt: instant, memberId: withId, override }) }), { write: true })
        : await call(
            () =>
              bookingApprovalsCreate({
                body: bookingBody({
                  clientId: client?.id ?? null,
                  newClient: creating ? { name: newName, phone: newPhone, email: newEmail } : null,
                  serviceId,
                  memberId: withId || null,
                  startsAt: instant,
                  override,
                }),
              }),
            { write: true },
          );
      const saved = outcome.data as { id: string; starts_at: string } | undefined;
      if ((outcome.status === 200 || outcome.status === 201) && saved) {
        onDone({ id: saved.id, startsAt: saved.starts_at, message: moving ? t("moved", { when: whenOf(saved.starts_at) }) : t("booked", { client: clientName, when: whenOf(saved.starts_at) }) });
        return;
      }
      const failure = submitFailure(outcome, { newClientEmail: creating && newEmail.trim() !== "" });
      if (failure === "signedOut") setSignedOut(true);
      else if (failure === "slotTaken" || failure === "slotUnavailable") {
        setBanner(failure === "slotTaken" ? t("slotTaken") : t("slotUnavailable", { member: withId ? withName(withId) : person("nameNotSet") }));
        clearTime();
        setRefetch((n) => n + 1);
      } else if (failure === "emailTaken" || failure === "emailInvalid") {
        setEmailError(failure === "emailTaken" ? t("emailTaken") : form("invalidEmail"));
        focusTarget.current = "bf-new-email";
      } else if (failure === "changed") setBanner(t("changedClose"));
      else if (failure === "ownerOnly") setBanner(errorsT("ownerOnly"));
      else setBanner(form(problem(outcome)));
    } catch {
      setBanner(form("unexpected"));
    } finally {
      setBusy(false);
      submitting.current = false;
    }
  }

  const term = searchTerm(q);
  const shown = results && term && results.term === term ? results.list : null;
  const contact = (c: ClientOut) => c.phone ?? c.email ?? "";

  const clientField = (
    <div className={uiStyles.field}>
      {client ? (
        <>
          <span className={uiStyles.label}>{t("clientLabel")}</span>
          <div className={css.picked}>
            <span>
              <b>{client.name}</b>
              {contact(client) && <small>{contact(client)}</small>}
            </span>
            <button
              className={uiStyles.textButton}
              type="button"
              onClick={() => {
                setClient(null);
                focusTarget.current = "bf-client";
              }}
            >
              {t("clientChange")}
            </button>
          </div>
        </>
      ) : creating ? (
        <>
          <span className={uiStyles.label}>{t("clientLabel")}</span>
          <div className={uiStyles.field}>
            <label className={uiStyles.label} htmlFor="bf-new-name">
              {t("newClientName")}
            </label>
            <div className={uiStyles.input}>
              <input id="bf-new-name" type="text" maxLength={100} autoComplete="off" value={newName} onChange={(event) => setNewName(event.target.value)} />
            </div>
          </div>
          <div className={uiStyles.field}>
            <label className={uiStyles.label} htmlFor="bf-new-phone">
              {t("newClientPhone")}
            </label>
            <div className={uiStyles.input}>
              <input id="bf-new-phone" type="tel" maxLength={40} autoComplete="off" value={newPhone} onChange={(event) => setNewPhone(event.target.value)} />
            </div>
          </div>
          <div className={uiStyles.field}>
            <label className={uiStyles.label} htmlFor="bf-new-email">
              {t("newClientEmail")}
            </label>
            <div className={uiStyles.input}>
              <input
                id="bf-new-email"
                type="email"
                autoComplete="off"
                value={newEmail}
                aria-invalid={emailError ? true : undefined}
                aria-describedby={emailError ? "bf-new-email-error" : "bf-new-email-hint"}
                onChange={(event) => {
                  setNewEmail(event.target.value);
                  setEmailError(null);
                }}
              />
            </div>
            {emailError ? (
              <FieldError id="bf-new-email-error">{emailError}</FieldError>
            ) : (
              <p className={uiStyles.hint} id="bf-new-email-hint">
                {t("newClientEmailHint")}
              </p>
            )}
          </div>
          <button
            className={uiStyles.textButton}
            type="button"
            onClick={() => {
              setCreating(false);
              setEmailError(null);
              focusTarget.current = "bf-client";
            }}
          >
            {t("searchInstead")}
          </button>
        </>
      ) : (
        <>
          <label className={uiStyles.label} htmlFor="bf-client">
            {t("clientLabel")}
          </label>
          <div className={uiStyles.input}>
            <input id="bf-client" type="text" autoComplete="off" placeholder={t("clientSearch")} aria-describedby="bf-client-count" value={q} onChange={(event) => setQ(event.target.value)} />
          </div>
          <p className={uiStyles.srOnly} id="bf-client-count" role="status" aria-live="polite">
            {shown ? (shown.length > 0 ? t("clientResults", { count: shown.length }) : t("clientNoneFound")) : ""}
          </p>
          {term && (
            <ul className={css.results}>
              {shown?.map((c) => (
                <li key={c.id}>
                  <button
                    type="button"
                    onClick={() => {
                      setClient(c);
                      focusTarget.current = "bf-service";
                    }}
                  >
                    <b>{c.name}</b>
                    {contact(c) && <small>{contact(c)}</small>}
                  </button>
                </li>
              ))}
              {shown?.length === 0 && <li className={css.noResults}>{t("clientNoneFound")}</li>}
              <li>
                <button
                  type="button"
                  className={css.newClient}
                  onClick={() => {
                    setCreating(true);
                    setNewName(q.trim());
                    focusTarget.current = "bf-new-name";
                  }}
                >
                  {t("clientNew", { q: q.trim() })}
                </button>
              </li>
            </ul>
          )}
        </>
      )}
    </div>
  );

  return (
    <form className={uiStyles.stack} noValidate onSubmit={submit}>
      {signedOut && <SignedOutBanner />}
      {moving && <p className={uiStyles.hint}>{t("moveFor", { client: booking.client_name, service: serviceLabel })}</p>}
      {!moving && clientField}

      {!moving && (
        <div className={uiStyles.field}>
          <label className={uiStyles.label} htmlFor="bf-service">
            {t("serviceLabel")}
          </label>
          <div className={uiStyles.input}>
            <select
              id="bf-service"
              required
              value={serviceId}
              onChange={(event) => {
                setServiceId(event.target.value);
                clearTime();
              }}
            >
              <option value="" disabled>
                {t("chooseService")}
              </option>
              {services.map((s) => (
                <option key={s.id} value={s.id}>
                  {t("serviceOption", { name: ownService(s.name), minutes: s.duration_minutes, price: formatMoney(s.price.amount_minor, s.price.currency, locale) })}
                </option>
              ))}
            </select>
          </div>
          {prefill.with && isOwner && !defaultService(services, prefill.with) && <Banner tone="note">{t("memberNoService", { member: prefillMember })}</Banner>}
        </div>
      )}

      {isOwner && (
        <div className={uiStyles.field}>
          <label className={uiStyles.label} htmlFor="bf-with">
            {t("withLabel")}
          </label>
          <div className={uiStyles.input}>
            <select
              id="bf-with"
              value={withId}
              onChange={(event) => {
                setWithId(event.target.value);
                clearTime();
              }}
            >
              {!moving && <option value="">{t("anyoneFree")}</option>}
              {workers.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.display_name ?? person("nameNotSet")}
                </option>
              ))}
            </select>
          </div>
        </div>
      )}

      {serviceId && (
        <>
          <div className={uiStyles.field}>
            <span className={uiStyles.label} id="bf-day-label">
              {t("dayLabel")}
            </span>
            <div className={css.strip} role="group" aria-labelledby="bf-day-label">
              <button
                type="button"
                className={css.stripStep}
                aria-label={t("prevDays")}
                disabled={anchor <= todayISO}
                onClick={() => {
                  setAnchor(shiftStrip(anchor, -1, todayISO));
                  setDay(null);
                }}
              >
                <span aria-hidden="true">{"‹"}</span>
              </button>
              {days.map((d) => (
                <button
                  key={d}
                  type="button"
                  className={css.chipBtn}
                  aria-pressed={d === selectedDay}
                  aria-label={longDay.format(asDate(d))}
                  onClick={() => {
                    setDay(d);
                    clearTime();
                  }}
                >
                  {stripLabel.format(asDate(d))}
                </button>
              ))}
              <button
                type="button"
                className={css.stripStep}
                aria-label={t("nextDays")}
                onClick={() => {
                  setAnchor(shiftStrip(anchor, 1, todayISO));
                  setDay(null);
                }}
              >
                <span aria-hidden="true">{"›"}</span>
              </button>
            </div>
          </div>

          <div className={uiStyles.field} aria-busy={loading || undefined}>
            <span className={uiStyles.label} id="bf-time-label">
              {t("timeLabel")}
            </span>
            {failedAvailability ? (
              <LoadFailure failure={failedAvailability.problem} onRetry={() => setRefetch((n) => n + 1)} />
            ) : loading ? (
              <p className={uiStyles.hint} role="status">
                {t("timesLoading")}
              </p>
            ) : times.length > 0 ? (
              <div className={css.times} role="group" aria-labelledby="bf-time-label">
                {times.map((slot) => (
                  <button key={slot} type="button" className={css.chipBtn} aria-pressed={pick.kind === "slot" && Date.parse(pick.slot) === Date.parse(slot)} onClick={() => choose({ kind: "slot", slot })}>
                    {localTime(slot, tz)}
                  </button>
                ))}
              </div>
            ) : (
              <p className={uiStyles.hint}>{t("noTimes")}</p>
            )}
          </div>

          <div className={uiStyles.field}>
            <button
              className={uiStyles.textButton}
              type="button"
              aria-expanded={pick.kind === "other"}
              aria-controls="bf-other"
              onClick={() => choose(pick.kind === "other" ? { kind: "none" } : { kind: "other", date: selectedDay, time: pick.kind === "slot" ? localTime(pick.slot, tz) : "" })}
            >
              {t("pickOther")}
            </button>
            {pick.kind === "other" && (
              <div id="bf-other" className={uiStyles.stack}>
                <p className={uiStyles.hint}>{t("pickOtherHint")}</p>
                <div className={css.otherRow}>
                  <div className={uiStyles.field}>
                    <label className={uiStyles.label} htmlFor="bf-other-date">
                      {t("otherDate")}
                    </label>
                    <div className={uiStyles.input}>
                      <input id="bf-other-date" type="date" value={pick.date} onChange={(event) => choose({ ...pick, date: event.target.value })} />
                    </div>
                  </div>
                  <div className={uiStyles.field}>
                    <label className={uiStyles.label} htmlFor="bf-other-time">
                      {t("otherTime")}
                    </label>
                    <div className={uiStyles.input}>
                      <input id="bf-other-time" type="time" value={pick.time} onChange={(event) => choose({ ...pick, time: event.target.value })} />
                    </div>
                  </div>
                </div>
                {needsPerson && <FieldError id="bf-person-error">{t("pickPerson")}</FieldError>}
              </div>
            )}
          </div>
        </>
      )}

      {banner && <Banner tone="error">{banner}</Banner>}

      {instant && (
        <p className={uiStyles.hint}>{withId ? t("summaryWith", { when: whenOf(instant), member: withName(withId) }) : t("summaryAnyone", { when: whenOf(instant) })}</p>
      )}
      <button className={`${uiStyles.button} ${uiStyles.primary}`} type="submit" disabled={!complete} aria-disabled={busy || undefined}>
        {busy ? form("sending") : instant ? (moving ? (isToday(instant) ? t("moveToday", { time: localTime(instant, tz) }) : t("moveDate", { date: dateFormat.format(new Date(instant)), time: localTime(instant, tz) })) : isToday(instant) ? t("bookToday", { service: serviceLabel, time: localTime(instant, tz) }) : t("bookDate", { service: serviceLabel, date: dateFormat.format(new Date(instant)), time: localTime(instant, tz) })) : moving ? t("moveTitle") : t("newTitle")}
      </button>
      {moving ? (
        clientEmail && <p className={uiStyles.hint}>{t("moveHint", { client: clientName })}</p>
      ) : (
        <p className={uiStyles.hint}>
          {t("confirmedNow")}
          {clientEmail ? ` ${t("confirmedEmail", { client: clientName })}` : ""}
        </p>
      )}
    </form>
  );
}
