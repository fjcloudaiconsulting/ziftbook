"use client";

import { useRouter } from "next/navigation";
import Script from "next/script";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { availabilityRead, bookingsCreate, type BookingPageOut, sessionRead } from "@/api-client";
import { addDays, localDay, localWhen, slotsByDay, weekDays } from "@/lib/booking-link";
import {
  answerState,
  bookingBody,
  cancellationState,
  emailSuggestion,
  firstFreeDayFrom,
  groupByDayPart,
  nextWeekDisabled,
  ownPolicyText,
  scanWindow,
} from "@/lib/booking-page";
import { dateLocale } from "@/lib/console";
import { formatMoney } from "@/lib/money";
import { type Locale, serviceName } from "@/lib/services";
import { zoneCity } from "@/lib/week";

import { Banner, FieldError, Outcome, send, Submit } from "../_ui/parts";
import styles from "../_ui/ui.module.css";

type Service = BookingPageOut["services"][number];
type Worker = Service["workers"][number];

/** The dynamic segment's fallback name+lang: reader locale, else the business's, else null (never
 * shown). `lang` is only set when the shown text is NOT in the reader's own locale (spec §4). */
function localized(map: Record<string, string>, locale: Locale, businessLanguage: Locale): { text: string; lang: Locale | null } {
  const text = serviceName(map, locale, businessLanguage);
  return { text, lang: map[locale] ? null : businessLanguage };
}

const TURNSTILE_LANG: Record<string, string> = { pt: "pt-br" };

// One flat shape for the whole flow (not a discriminated union): `service` is the only field that
// really gates step 2/3, and every call site already checks it (or `flow.step`) at runtime, so a
// union bought type-narrowing this file never used, at the cost of casts on every update.
type Flow = {
  step: 1 | 2 | 3;
  service: Service | null;
  worker: string;
  weekStart: string;
  day: string | null;
  slot: string | null;
  taken: Set<string>;
  name: string;
  email: string;
  phone: string;
  errors: { name?: string; email?: string };
  suggest: string | null;
  banner: { where: 1 | 2 | "c"; tone: "note" | "error"; text: string } | null;
  busy: boolean;
};
// Once `service` is set (steps 2 and 3), these fields are meaningful; this alias just documents
// that at call sites instead of re-asserting it.
type Step2Plus = Flow & { service: Service };

const INITIAL: Omit<Flow, "name" | "email" | "phone"> = {
  step: 1,
  service: null,
  worker: "any",
  weekStart: "",
  day: null,
  slot: null,
  taken: new Set(),
  errors: {},
  suggest: null,
  banner: null,
  busy: false,
};

type Done = { status: string; service: Service; worker: Worker | null; starts_at: string; price: { amount_minor: number; currency: string }; email: string };

type CacheEntry = { slots: string[] } | "error" | "loading";

export function BookingPage({ page, locale, turnstileSiteKey }: { page: BookingPageOut; locale: string; turnstileSiteKey: string | null }) {
  const t = useTranslations("BookingPage");
  const router = useRouter();
  const loc = locale as Locale;
  const zone = page.timezone;
  const businessLanguage = page.language as Locale;
  const dLocale = dateLocale(locale);

  const [flow, setFlow] = useState<Flow>({ ...INITIAL, name: "", email: "", phone: "" });
  const [done, setDone] = useState<Done | null>(null);
  // State, not a ref: read during render (the picker), so it must trigger a re-render on change.
  const [cache, setCache] = useState(new Map<string, CacheEntry>());
  const scanned = useRef(new Set<string>()); // service|worker keys already auto-scanned once
  // The most recent ensureWeek call's own number, per service|worker: an older call's scan can
  // still be in flight when the user pages to a different week before it resolves, and its result
  // (computed for the week IT was called with) must not overwrite what a newer call already wrote.
  const ensureWeekToken = useRef(new Map<string, number>());
  const working = useRef(false);
  const nameRef = useRef<HTMLInputElement>(null);
  const emailRef = useRef<HTMLInputElement>(null);
  const headingRefs = useRef<Record<number, HTMLHeadingElement | null>>({});
  const [focusStep, setFocusStep] = useState<number | null>(null);
  const [focusField, setFocusField] = useState<"name" | "email" | null>(null);
  const [verifying, setVerifying] = useState(false);
  const [turnstileToken, setTurnstileToken] = useState<string | null>(null);
  const turnstileWidget = useRef<string | null>(null);
  const turnstileTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingSubmit = useRef(false);
  const [firstFreeByEntry, setFirstFreeByEntry] = useState(new Map<string, string>());
  // What autofill actually filled (if anything): "Book another appointment" restores exactly this,
  // never whatever the visitor went on to type over it.
  const sessionFill = useRef({ name: "", email: "" });
  // Always current, read from the Turnstile widget's own callback: that callback is created once
  // by onTurnstileLoad and would otherwise close over the render's doBook from that moment (stale
  // — in particular, one from before any service was ever chosen).
  const doBookRef = useRef<(token: string | null) => Promise<void>>(async () => {});

  // Autofill: GET /api/session once; fill name/email ONLY if still empty. Any other answer (no
  // session, network, whatever) is ignored silently.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const answer = await send(sessionRead());
      if (cancelled || answer.status !== 200 || !answer.data) return;
      setFlow((f) => {
        const name = f.name || answer.data!.display_name || f.name;
        const email = f.email || answer.data!.email || f.email;
        sessionFill.current = { name: f.name ? sessionFill.current.name : name, email: f.email ? sessionFill.current.email : email };
        return { ...f, name, email };
      });
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (focusStep !== null) headingRefs.current[focusStep]?.focus();
  }, [focusStep]);

  // Runs after step 3 has (re)rendered with its inputs mounted, so a field targeted while step 3
  // wasn't the open step still gets focused once it is.
  useEffect(() => {
    if (focusField === "name") nameRef.current?.focus();
    else if (focusField === "email") emailRef.current?.focus();
  }, [focusField, flow.step]);

  function cacheKey(service: string, worker: string, from: string) {
    return `${service}|${worker}|${from}`;
  }

  /** Sends the client back to step 1 with a note, and re-fetches the page's own data (a service
   * that just 404'd may be gone from `page` itself too). Used for both a 404 on POST /bookings
   * and a 404 on the week GET — the spec gives both the same path. */
  function backToStep1(noteText: string) {
    router.refresh();
    setFlow((f) => ({ ...INITIAL, name: f.name, email: f.email, phone: f.phone, banner: { where: 1, tone: "note", text: noteText } }));
    setFocusStep(1);
  }

  /** The 14-day window starting at `from`, cached so paging within it needs no new fetch. Never
   * resolves to "loading": that value only ever marks an in-flight fetch in the cache map itself.
   * `bypass`: skip a cached entry and fetch fresh (after a 409, the cached window is stale). */
  async function loadWindow(service: Service, worker: string, from: string, bypass = false): Promise<{ slots: string[] } | "error"> {
    const key = cacheKey(service.id, worker, from);
    if (!bypass) {
      const existing = cache.get(key);
      if (existing && existing !== "loading") return existing;
    }
    setCache((m) => new Map(m).set(key, "loading"));
    const { from: qFrom, to } = scanWindow(from);
    const answer = await send(
      availabilityRead({ path: { tenant_id: page.id, service_id: service.id }, query: { from: qFrom, to, member_id: worker === "any" ? null : worker } }),
    );
    if (answer.status === 200 && answer.data) {
      const entry: CacheEntry = { slots: answer.data.slots };
      setCache((m) => new Map(m).set(key, entry));
      return entry;
    }
    if (answer.status === 404) {
      // The service (or every worker on it) is gone: same path as a 404 on Book (spec's mapping).
      backToStep1(t("serviceGone"));
      return "error";
    }
    setCache((m) => new Map(m).set(key, "error"));
    return "error";
  }

  /** Which 14-day window a given day falls in, given the entry window's own `from`. */
  function windowFromFor(entryFrom: string, day: string): string {
    let from = entryFrom;
    while (addDays(from, 13) < day) from = addDays(from, 14);
    while (day < from) from = addDays(from, -14);
    return from;
  }

  async function ensureWeek(service: Service, worker: string, weekStart: string, bypass = false) {
    const entryKey = `${service.id}|${worker}`;
    const myToken = (ensureWeekToken.current.get(entryKey) ?? 0) + 1;
    ensureWeekToken.current.set(entryKey, myToken);
    const isCurrent = () => ensureWeekToken.current.get(entryKey) === myToken;

    const windowFrom = windowFromFor(weekStart, weekStart);
    const entry = await loadWindow(service, worker, windowFrom, bypass);
    if (entry === "error" || !isCurrent()) return;

    const visibleWeekEmpty = weekDays(weekStart).every((d) => !(slotsByDay(entry.slots, zone).get(d) ?? []).length);
    if (!visibleWeekEmpty) return;

    // B8: what's already in hand (this SAME 14-day fetch covers the following week too) is
    // checked before any further request — a free day may already be sitting in `entry.slots`.
    let first = firstFreeDayFrom(entry.slots, weekStart, zone);
    if (first === null && !scanned.current.has(entryKey)) {
      scanned.current.add(entryKey);
      let from = windowFrom;
      const businessToday = localDay(new Date(), zone);
      for (let i = 0; i < 5; i++) {
        from = addDays(from, 14);
        if (nextWeekDisabled(businessToday, from, page.booking_horizon_days)) break;
        const next = await loadWindow(service, worker, from);
        if (next === "error" || !isCurrent()) return;
        first = firstFreeDayFrom(next.slots, from, zone);
        if (first !== null) break;
      }
    }
    if (!isCurrent()) return; // superseded by a newer call for this service/worker while awaiting
    setFirstFreeByEntry((m) => new Map(m).set(entryKey, first ?? ""));
  }

  /** A genuinely different service resets worker/day/slot; re-confirming the one already picked
   * (clicking it again after "Change") just moves on without losing what was already chosen. */
  function selectService(service: Service) {
    if (flow.service?.id === service.id) {
      const step = flow.slot ? 3 : 2;
      setFlow((f) => ({ ...f, step }));
      setFocusStep(step);
      return;
    }
    const worker = service.workers.length === 1 ? service.workers[0].id : "any";
    const weekStart = localDay(new Date(), zone);
    setFlow((f) => ({ ...f, step: 2, service, worker, weekStart, day: null, slot: null, taken: new Set(), banner: null }));
    setFocusStep(2);
    void ensureWeek(service, worker, weekStart);
  }

  function chooseWorker(worker: string) {
    setFlow((f) => (f.step >= 2 ? { ...f, worker, day: null, slot: null } : f));
    const f = flow as Step2Plus;
    void ensureWeek(f.service, worker, f.weekStart);
  }

  function changeWeek(delta: number) {
    setFlow((f) => (f.step >= 2 ? { ...f, weekStart: addDays(f.weekStart, delta), day: null } : f));
    const f = flow as Step2Plus;
    void ensureWeek(f.service, f.worker, addDays(f.weekStart, delta));
  }

  function jumpTo(day: string) {
    const back = (new Date(`${day}T12:00:00Z`).getUTCDay() + 6) % 7;
    const today = localDay(new Date(), zone);
    const weekStart = addDays(day, -back) < today ? today : addDays(day, -back);
    setFlow((f) => (f.step >= 2 ? { ...f, weekStart, day } : f));
    const f = flow as Step2Plus;
    void ensureWeek(f.service, f.worker, weekStart);
  }

  function pickSlot(slot: string) {
    setFlow((f) => (f.step >= 2 ? { ...f, slot, step: 3 } : f));
    setFocusStep(3);
  }

  function goTo(step: 1 | 2 | 3) {
    setFlow((f) => ({ ...f, step }));
    setFocusStep(step);
  }

  function fieldChange(field: "name" | "email" | "phone", value: string) {
    setFlow((f) => {
      const next = { ...f, [field]: value };
      if ((field === "name" || field === "email") && f.errors[field]) next.errors = { ...f.errors, [field]: undefined };
      // A suggestion computed from the email as it was at the last blur is stale the moment the
      // value changes again — clicking "Use this address" afterward must never apply it.
      if (field === "email" && f.suggest) next.suggest = null;
      return next;
    });
  }

  function emailBlur() {
    setFlow((f) => ({ ...f, suggest: emailSuggestion(f.email) }));
  }

  function useSuggestion() {
    setFlow((f) => ({ ...f, email: f.suggest ?? f.email, suggest: null, errors: { ...f.errors, email: undefined } }));
  }

  // --- Turnstile ---
  // A client-side locale switch or slug nav unmounts this component with the widget still live;
  // removing it explicitly (Cloudflare's own recommendation) avoids leaking it and the console
  // warning that follows from just letting its container be torn out from under it.
  useEffect(() => {
    return () => {
      const w = (window as unknown as { turnstile?: { remove(id: string): void } }).turnstile;
      if (w?.remove && turnstileWidget.current) w.remove(turnstileWidget.current);
    };
  }, []);

  function onTurnstileLoad() {
    const w = (window as unknown as { turnstile?: { render(el: string | Element, opts: Record<string, unknown>): string; reset(id?: string): void } }).turnstile;
    const el = document.getElementById("turnstile-container");
    if (!w || !el) return;
    turnstileWidget.current = w.render(el, {
      sitekey: turnstileSiteKey,
      "response-field": false,
      appearance: "interaction-only",
      language: TURNSTILE_LANG[locale] ?? locale,
      callback: (token: string) => {
        setTurnstileToken(token);
        setVerifying(false);
        if (turnstileTimer.current) clearTimeout(turnstileTimer.current);
        if (pendingSubmit.current) {
          pendingSubmit.current = false;
          void doBookRef.current(token);
        }
      },
      "error-callback": () => {
        setVerifying(false);
        pendingSubmit.current = false;
        if (turnstileTimer.current) {
          clearTimeout(turnstileTimer.current);
          turnstileTimer.current = null;
        }
        setFlow((f) => ({ ...f, banner: { where: "c", tone: "error", text: t("verifyTimeout") } }));
      },
    });
  }

  function resetTurnstile() {
    setTurnstileToken(null);
    const w = (window as unknown as { turnstile?: { reset(id?: string): void } }).turnstile;
    if (w && turnstileWidget.current) w.reset(turnstileWidget.current);
  }

  async function book() {
    if (working.current) return;
    // Not gated by flow.step: a complete selection (service + slot) is still complete even while
    // step 1 or 2 happens to be reopened via "Change" — the summary already shows it.
    if (!flow.service || !flow.slot) {
      setFlow((f) => ({ ...f, banner: { where: "c", tone: "error", text: f.service ? t("errMissingTime") : t("errMissing") } }));
      goTo(flow.service ? 2 : 1);
      return;
    }
    const errors: Flow["errors"] = {};
    if (!flow.name.trim()) errors.name = t("errName");
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(flow.email.trim())) errors.email = t("errEmail");
    if (Object.keys(errors).length > 0) {
      // Step 3's inputs only exist in the DOM while it's the open step: open it first (if it
      // wasn't already) so the ref below actually points at a mounted field, then focus once it
      // has — a step 2 "Change" reopen must not leave the error unfocusable.
      setFlow((f) => ({ ...f, errors, step: 3 }));
      setFocusField(errors.name ? "name" : "email");
      return;
    }
    if (turnstileSiteKey && !turnstileToken) {
      setVerifying(true);
      pendingSubmit.current = true;
      turnstileTimer.current = setTimeout(() => {
        if (pendingSubmit.current) {
          pendingSubmit.current = false;
          setVerifying(false);
          setFlow((f) => ({ ...f, banner: { where: "c", tone: "error", text: t("verifyTimeout") } }));
        }
      }, 10_000);
      return;
    }
    await doBook(turnstileToken);
  }

  async function doBook(token: string | null) {
    working.current = true;
    setFlow((f) => ({ ...f, busy: true, banner: null }));
    const f = flow as Step2Plus;
    const body = bookingBody({
      startsAt: f.slot!,
      memberId: f.worker === "any" ? null : f.worker,
      name: f.name,
      email: f.email,
      phone: f.phone,
      locale,
      policyVersion: page.policy_version,
      turnstileToken: token,
    });
    const answer = await send(bookingsCreate({ path: { tenant_id: page.id, service_id: f.service.id }, body: { ...body, locale: loc } }));
    working.current = false;
    // Reset after EVERY answer, 201 included: a fresh "Book another" must never reuse this token.
    if (turnstileSiteKey) resetTurnstile();

    const outcome = answerState(answer);
    if (outcome.kind === "done" && answer.data) {
      setDone({
        status: answer.data.status,
        service: f.service,
        worker: f.worker === "any" ? (f.service.workers.find((w) => w.id === answer.data!.worker_id) ?? null) : (f.service.workers.find((w) => w.id === f.worker) ?? null),
        starts_at: answer.data.starts_at,
        price: answer.data.price,
        email: f.email,
      });
      return;
    }
    if (outcome.kind === "slotTaken") {
      const time = localWhen(f.slot!, zone, dateLocale(locale)).time;
      setFlow({ ...f, slot: null, step: 2, taken: new Set([...f.taken, f.slot!]), busy: false, banner: { where: 2, tone: "note", text: t("slotTaken", { time }) } });
      setFocusStep(2);
      // The cached window is stale (it still offers the just-taken slot): bypass it.
      void ensureWeek(f.service, f.worker, f.weekStart, true);
      return;
    }
    if (outcome.kind === "serviceGone") {
      backToStep1(t("serviceGone"));
      return;
    }
    if (outcome.kind === "policyChanged") {
      router.refresh();
      setFlow({ ...f, busy: false, banner: { where: "c", tone: "error", text: t("policyChanged") } });
      return;
    }
    const text =
      outcome.kind === "verifyFailed"
        ? t("verifyFailed")
        : outcome.kind === "tooMany"
          ? t("tooMany")
          : outcome.kind === "nothingBooked"
            ? t("nothingBooked")
            : outcome.kind === "fieldErrors"
              ? t("checkDetails")
              : t("unknownOutcome");
    setFlow({ ...f, busy: false, banner: { where: "c", tone: "error", text } });
  }
  useEffect(() => {
    doBookRef.current = doBook;
  });

  const service1Done = flow.service;
  // flow.service is checked, not just flow.step: TS doesn't propagate that narrowing through the
  // ternary's inferred type, so the cast documents what the runtime check already guarantees.
  // Not gated by flow.step: the checkout summary (and step 2/3's own folded rows) must keep
  // showing what's already chosen even while step 1 is reopened via "Change".
  const s2 = flow.service ? (flow as Step2Plus) : null;

  if (page.services.length === 0) {
    return (
      <main className={styles.screen}>
        <div className={styles.col}>
          <Outcome icon="calendar" title={t("noSvcTitle")} lede={t("noSvcBody", { biz: page.name })}>
            {null}
          </Outcome>
        </div>
      </main>
    );
  }

  return (
    <>
    <main className={styles.screen}>
      <div className={`${styles.col} ${styles.bookingWide}`}>
        <div className={styles.bizHeader}>
          <h1>{page.name}</h1>
          <p className={styles.bizMeta}>
            <PinIcon />
            <span>{zoneCity(zone)}</span>
            <span aria-hidden="true">·</span>
            <span>{t("timesIn", { city: zoneCity(zone) })}</span>
          </p>
        </div>

        <div className={styles.bookingLayout}>
          <div className={styles.flowCol}>
            {done ? (
              <DoneScreen page={page} done={done} locale={loc} dLocale={dLocale} zone={zone} t={t} onAgain={() => { setDone(null); setFlow({ ...INITIAL, name: sessionFill.current.name, email: sessionFill.current.email, phone: "" }); }} />
            ) : (
              <>
              {/* Step 1: service */}
              <section className={`${styles.step} ${flow.step !== 1 && !(flow.step > 1 && service1Done) ? styles.stepLater : ""}`} aria-labelledby="step1-h">
                <div className={styles.stepHead}>
                  <span className={`${styles.stepNum} ${flow.step === 1 ? styles.stepNumOpen : flow.step > 1 && service1Done ? styles.stepNumDone : ""}`} aria-hidden="true">
                    {flow.step > 1 && service1Done ? <CheckIcon /> : 1}
                  </span>
                  <h2 id="step1-h" ref={(el) => { headingRefs.current[1] = el; }} tabIndex={-1}>
                    {t("step1")}
                    {flow.step > 1 && service1Done && <span className={styles.srOnly}>{t("stepDoneSuffix")}</span>}
                  </h2>
                </div>
                <div className={styles.stepBody}>
                  {flow.banner?.where === 1 && <Banner tone={flow.banner.tone}>{flow.banner.text}</Banner>}
                  {flow.step === 1 ? (
                    <ul className={styles.services} role="radiogroup" aria-labelledby="step1-h">
                      {page.services.map((svc) => {
                        const name = localized(svc.name, loc, businessLanguage);
                        const desc = svc.description[loc] || svc.description[businessLanguage] ? localized(svc.description, loc, businessLanguage) : null;
                        return (
                          <li key={svc.id}>
                            <label className={styles.svcChoice}>
                              <input type="radio" name="service" checked={flow.service?.id === svc.id} readOnly onClick={() => selectService(svc)} />
                              <span className={styles.svcMain}>
                                <span className={styles.svcTop}>
                                  <span className={styles.svcName} lang={name.lang ?? undefined}>
                                    {name.text}
                                  </span>
                                  <span className={styles.svcPrice}>{formatMoney(svc.price.amount_minor, svc.price.currency, locale)}</span>
                                </span>
                                {desc && (
                                  <span className={styles.svcDesc} lang={desc.lang ?? undefined}>
                                    {desc.text}
                                  </span>
                                )}
                                <span className={styles.svcMeta}>
                                  <ClockIcon />
                                  {t("min", { n: svc.duration_minutes })}
                                </span>
                              </span>
                            </label>
                          </li>
                        );
                      })}
                    </ul>
                  ) : (
                    service1Done && (
                      <div className={styles.stepDone}>
                        <span className={styles.stepDoneWhat}>
                          <b lang={localized(service1Done.name, loc, businessLanguage).lang ?? undefined}>{localized(service1Done.name, loc, businessLanguage).text}</b>
                          <span>
                            {t("min", { n: service1Done.duration_minutes })} · {formatMoney(service1Done.price.amount_minor, service1Done.price.currency, locale)}
                          </span>
                        </span>
                        <button className={styles.textButton} type="button" onClick={() => goTo(1)}>
                          {t("change")}
                          <span className={styles.srOnly}>
                            {": "}
                            {t("step1")}
                          </span>
                        </button>
                      </div>
                    )
                  )}
                </div>
              </section>

              {/* Step 2: day/time — the shell always renders (muted) even before step 1 is done. */}
              <section className={`${styles.step} ${flow.step !== 2 && !(flow.step > 2 && s2?.slot) ? styles.stepLater : ""}`} aria-labelledby="step2-h">
                <div className={styles.stepHead}>
                  <span className={`${styles.stepNum} ${flow.step === 2 ? styles.stepNumOpen : flow.step > 2 && s2?.slot ? styles.stepNumDone : ""}`} aria-hidden="true">
                    {flow.step > 2 && s2?.slot ? <CheckIcon /> : 2}
                  </span>
                  <h2 id="step2-h" ref={(el) => { headingRefs.current[2] = el; }} tabIndex={-1}>
                    {t("step2")}
                    {flow.step > 2 && <span className={styles.srOnly}>{t("stepDoneSuffix")}</span>}
                  </h2>
                </div>
                <div className={styles.stepBody}>
                  {s2 && flow.step === 2 && (
                    <Picker
                      page={page}
                      s2={s2}
                      cache={cache}
                      firstFree={firstFreeByEntry.get(`${s2.service.id}|${s2.worker}`) || null}
                      banner={flow.banner?.where === 2 ? flow.banner : null}
                      dLocale={dLocale}
                      zone={zone}
                      t={t}
                      onWorker={chooseWorker}
                      onWeek={changeWeek}
                      onDay={(d) => setFlow((f) => (f.step >= 2 ? { ...f, day: d } : f))}
                      onSlot={pickSlot}
                      onJump={jumpTo}
                      onAnyone={() => chooseWorker("any")}
                      onRetry={() => void ensureWeek(s2.service, s2.worker, s2.weekStart, true)}
                    />
                  )}
                  {s2 && flow.step > 2 && s2.slot && (
                    <div className={styles.stepDone}>
                      <span className={styles.stepDoneWhat}>
                        <b>
                          {localWhen(s2.slot, zone, dLocale).date}
                          {", "}
                          {localWhen(s2.slot, zone, dLocale).time}
                        </b>
                        <span>{t("with", { name: s2.worker === "any" ? t("anyone") : (s2.service.workers.find((w) => w.id === s2.worker)?.display_name ?? "") })}</span>
                      </span>
                      <button className={styles.textButton} type="button" onClick={() => goTo(2)}>
                        {t("change")}
                        <span className={styles.srOnly}>
                          {": "}
                          {t("step2")}
                        </span>
                      </button>
                    </div>
                  )}
                </div>
              </section>

              {/* Step 3: details — same always-visible shell. */}
              <section className={`${styles.step} ${flow.step !== 3 ? styles.stepLater : ""}`} aria-labelledby="step3-h">
                <div className={styles.stepHead}>
                  <span className={`${styles.stepNum} ${flow.step === 3 ? styles.stepNumOpen : ""}`} aria-hidden="true">
                    {3}
                  </span>
                  <h2 id="step3-h" ref={(el) => { headingRefs.current[3] = el; }} tabIndex={-1}>
                    {t("step3")}
                  </h2>
                </div>
                <div className={styles.stepBody}>
                {s2 && flow.step === 3 && (
                  <div className={styles.form}>
                    <div className={styles.row2}>
                      <div className={styles.field}>
                        <label className={styles.label} htmlFor="bp-name">
                          {t("name")}
                        </label>
                        <div className={styles.input}>
                          <input
                            ref={nameRef}
                            id="bp-name"
                            name="name"
                            autoComplete="name"
                            value={flow.name}
                            onChange={(e) => fieldChange("name", e.target.value)}
                            aria-invalid={flow.errors.name ? true : undefined}
                            aria-describedby={flow.errors.name ? "bp-name-err" : undefined}
                          />
                        </div>
                        {flow.errors.name && <FieldError id="bp-name-err">{flow.errors.name}</FieldError>}
                      </div>
                      <div className={styles.field}>
                        <label className={styles.label} htmlFor="bp-phone">
                          {t("phone")} <span className={styles.hint}>{t("optional")}</span>
                        </label>
                        <div className={styles.input}>
                          <input
                            id="bp-phone"
                            name="phone"
                            type="tel"
                            autoComplete="tel"
                            value={flow.phone}
                            onChange={(e) => fieldChange("phone", e.target.value)}
                            aria-describedby="bp-phone-hint"
                          />
                        </div>
                        <p className={styles.hint} id="bp-phone-hint">
                          {t("phoneHint")}
                        </p>
                      </div>
                    </div>
                    <div className={styles.field}>
                      <label className={styles.label} htmlFor="bp-email">
                        {t("email")}
                      </label>
                      <div className={styles.input}>
                        <input
                          ref={emailRef}
                          id="bp-email"
                          name="email"
                          type="email"
                          autoComplete="email"
                          required
                          value={flow.email}
                          onChange={(e) => fieldChange("email", e.target.value)}
                          onBlur={emailBlur}
                          aria-invalid={flow.errors.email ? true : undefined}
                          aria-describedby={`bp-email-hint${flow.errors.email ? " bp-email-err" : ""}`}
                        />
                      </div>
                      <p className={styles.hint} id="bp-email-hint">
                        {t("emailHint")}
                      </p>
                      {flow.errors.email && <FieldError id="bp-email-err">{flow.errors.email}</FieldError>}
                      <div aria-live="polite">
                        {flow.suggest && (
                          <p className={styles.hint}>
                            {t("didYouMean", { email: flow.suggest })}{" "}
                            <button className={styles.textButton} type="button" onClick={useSuggestion}>
                              {t("useSuggestion")}
                            </button>
                          </p>
                        )}
                      </div>
                    </div>
                  </div>
                )}
                </div>
              </section>
              </>
            )}
          </div>

          <Checkout
            page={page}
            s2={s2}
            locale={locale}
            dLocale={dLocale}
            zone={zone}
            businessLanguage={businessLanguage}
            t={t}
            banner={flow.banner?.where === "c" ? flow.banner : null}
            busy={flow.busy}
            verifying={verifying}
            turnstileSiteKey={turnstileSiteKey}
            onTurnstileLoad={onTurnstileLoad}
            onBook={() => void book()}
            hidden={done !== null}
          />
        </div>
      </div>
    </main>
    <footer className={styles.minimalFooter}>
      <span>
        {t("footerBy")} <b>ziftbook</b>
      </span>
    </footer>
    </>
  );
}

function PinIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 16 16" width="15" height="15">
      <path d="M8 14s4.5-4.1 4.5-7.5a4.5 4.5 0 0 0-9 0C3.5 9.9 8 14 8 14z" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <circle cx="8" cy="6.5" r="1.6" fill="currentColor" />
    </svg>
  );
}

function ClockIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 16 16" width="14" height="14">
      <circle cx="8" cy="8" r="6.25" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <path d="M8 4.8V8l2.1 1.3" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 16 16" width="16" height="16">
      <path d="M3 8.5l3 3 7-7" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function AlertIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 16 16" width="16" height="16">
      <circle cx="8" cy="8" r="6.5" fill="none" stroke="currentColor" strokeWidth="1.6" />
      <path d="M8 4.5v4M8 11h.01" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}

function MoveIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 16 16" width="15" height="15">
      <path d="M2.5 5.5h9l-2.5-2.5M13.5 10.5h-9l2.5 2.5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function Picker({
  page,
  s2,
  cache,
  firstFree,
  banner,
  dLocale,
  zone,
  t,
  onWorker,
  onWeek,
  onDay,
  onSlot,
  onJump,
  onAnyone,
  onRetry,
}: {
  page: BookingPageOut;
  s2: Step2Plus;
  cache: Map<string, CacheEntry>;
  firstFree: string | null;
  banner: { tone: "note" | "error"; text: string } | null;
  dLocale: string;
  zone: string;
  t: ReturnType<typeof useTranslations>;
  onWorker(id: string): void;
  onWeek(delta: number): void;
  onDay(day: string): void;
  onSlot(slot: string): void;
  onJump(day: string): void;
  onAnyone(): void;
  onRetry(): void;
}) {
  const { service, worker } = s2;
  const today = localDay(new Date(), zone);
  const days = weekDays(s2.weekStart);
  const dayLabel = (day: string, options: Intl.DateTimeFormatOptions) =>
    new Intl.DateTimeFormat(dLocale, { ...options, timeZone: "UTC" }).format(new Date(`${day}T12:00:00Z`));

  let from = s2.weekStart;
  while (addDays(from, 13) < s2.weekStart) from = addDays(from, 14);
  while (s2.weekStart < from) from = addDays(from, -14);
  const entry = cache.get(`${service.id}|${worker}|${from}`);

  const nextDisabled = nextWeekDisabled(today, addDays(s2.weekStart, 7), page.booking_horizon_days);

  return (
    <div className={styles.picker}>
      {banner && <Banner tone={banner.tone}>{banner.text}</Banner>}
      {service.workers.length > 1 && (
        <fieldset className={styles.pills}>
          <legend className={styles.hint}>{t("withWho")}</legend>
          <div className={styles.pills}>
            <label className={styles.pill2}>
              <input type="radio" name="worker" checked={worker === "any"} onChange={() => onWorker("any")} />
              {worker === "any" && "✓ "}
              {t("anyone")}
            </label>
            {service.workers.map((w) => (
              <label className={styles.pill2} key={w.id}>
                <input type="radio" name="worker" checked={worker === w.id} onChange={() => onWorker(w.id)} />
                {worker === w.id && "✓ "}
                {w.display_name}
              </label>
            ))}
          </div>
        </fieldset>
      )}
      {service.workers.length === 1 && <p className={styles.hint}>{t("with", { name: service.workers[0].display_name ?? "" })}</p>}

      <div className={styles.weekHead}>
        <strong>
          {dayLabel(s2.weekStart, { day: "numeric", month: "short" })} – {dayLabel(addDays(s2.weekStart, 6), { day: "numeric", month: "short" })}
        </strong>
        <div className={styles.weekNav}>
          <button className={`${styles.button} ${styles.secondary} ${styles.small}`} type="button" disabled={s2.weekStart <= today} onClick={() => onWeek(-7)}>
            {t("prevWeek")}
          </button>
          <button className={`${styles.button} ${styles.secondary} ${styles.small}`} type="button" disabled={nextDisabled} onClick={() => onWeek(7)}>
            {t("nextWeek")}
          </button>
        </div>
      </div>

      {!entry || entry === "loading" ? (
        <p className={styles.hint} role="status">
          {t("loadingTimes")}
        </p>
      ) : entry === "error" ? (
        <>
          <Banner tone="error">{t("pickerError")}</Banner>
          <button className={styles.textButton} type="button" onClick={onRetry}>
            {t("tryAgain")}
          </button>
        </>
      ) : (
        <PickerWeek page={page} s2={s2} slots={entry.slots} firstFree={firstFree} today={today} days={days} dayLabel={dayLabel} zone={zone} dLocale={dLocale} t={t} onDay={onDay} onSlot={onSlot} onJump={onJump} onAnyone={onAnyone} />
      )}

      {nextDisabled && <p className={styles.hint}>{t("horizon", { n: page.booking_horizon_days })}</p>}
    </div>
  );
}

function PickerWeek({
  s2,
  slots,
  firstFree,
  today,
  days,
  dayLabel,
  zone,
  dLocale,
  t,
  onDay,
  onSlot,
  onJump,
  onAnyone,
}: {
  page: BookingPageOut;
  s2: Step2Plus;
  slots: string[];
  firstFree: string | null;
  today: string;
  days: string[];
  dayLabel(day: string, options: Intl.DateTimeFormatOptions): string;
  zone: string;
  dLocale: string;
  t: ReturnType<typeof useTranslations>;
  onDay(day: string): void;
  onSlot(slot: string): void;
  onJump(day: string): void;
  onAnyone(): void;
}) {
  const byDay = slotsByDay(slots, zone);
  const anyThisWeek = days.some((d) => (byDay.get(d) ?? []).length > 0);
  const day = s2.day && (byDay.get(s2.day) ?? []).length > 0 ? s2.day : (days.find((d) => (byDay.get(d) ?? []).length > 0) ?? null);

  // Selecting the week's first free day is a consequence of this render, not something to do
  // WHILE rendering (that would call the parent's setState mid-render): an effect, after.
  useEffect(() => {
    if (day && s2.day !== day) onDay(day);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [day]);

  if (!anyThisWeek) {
    const firstLabel = firstFree ? dayLabel(firstFree, { weekday: "long", day: "numeric", month: "long" }) : null;
    return (
      <div className={styles.empty} role="status">
        <strong>{t("noWeekTitle", { a: dayLabel(days[0], { weekday: "short", day: "numeric", month: "short" }), b: dayLabel(days[6], { weekday: "short", day: "numeric", month: "short" }) })}</strong>
        {firstLabel && <span>{t("noWeekBody", { first: firstLabel })}</span>}
        <div>
          {firstFree && firstLabel && (
            <button className={`${styles.button} ${styles.primary} ${styles.small}`} type="button" onClick={() => onJump(firstFree)}>
              {t("seeNext", { first: firstLabel })}
            </button>
          )}
          {s2.worker !== "any" && (
            <button className={`${styles.button} ${styles.secondary} ${styles.small}`} type="button" onClick={onAnyone}>
              {t("tryAnyone")}
            </button>
          )}
        </div>
      </div>
    );
  }

  if (!day) return null;
  const times = byDay.get(day) ?? [];
  const grouped = groupByDayPart(times, zone);

  return (
    <>
      <div className={styles.days} role="group" aria-label={`${dayLabel(days[0], { day: "numeric", month: "short" })} – ${dayLabel(days[6], { day: "numeric", month: "short" })}`}>
        {days.map((d) => {
          const has = (byDay.get(d) ?? []).length > 0;
          const long = dayLabel(d, { weekday: "long", day: "numeric", month: "long" });
          return (
            <button
              key={d}
              className={`${styles.button} ${d === day ? styles.primary : styles.secondary}`}
              type="button"
              disabled={!has || d < today}
              aria-pressed={d === day}
              aria-label={has ? long : `${long}, ${t("closed")}`}
              onClick={() => onDay(d)}
            >
              <span>{dayLabel(d, { weekday: "short" })}</span>
              <span>{dayLabel(d, { day: "numeric" })}</span>
            </button>
          );
        })}
      </div>
      <div className={styles.parts} role="group" aria-label={t("timesOn", { day: dayLabel(day, { weekday: "long", day: "numeric", month: "long" }) })}>
        {(["morning", "afternoon", "evening"] as const)
          .filter((part) => grouped[part].length > 0)
          .map((part) => (
            <div key={part}>
              <h3>{t(part)}</h3>
              <div className={styles.slots}>
                {grouped[part].map((slot) => {
                  const time = localWhen(slot, zone, dLocale).time;
                  const on = s2.slot === slot;
                  const struck = s2.taken.has(slot);
                  return (
                    <button
                      key={slot}
                      className={`${styles.button} ${styles.secondary}`}
                      type="button"
                      disabled={struck}
                      aria-pressed={struck ? undefined : on}
                      aria-label={struck ? t("takenLabel", { time }) : undefined}
                      onClick={() => onSlot(slot)}
                    >
                      {time}
                    </button>
                  );
                })}
              </div>
            </div>
          ))}
      </div>
    </>
  );
}

function Checkout({
  page,
  s2,
  locale,
  dLocale,
  zone,
  businessLanguage,
  t,
  banner,
  busy,
  verifying,
  turnstileSiteKey,
  onTurnstileLoad,
  onBook,
  hidden,
}: {
  page: BookingPageOut;
  s2: Step2Plus | null;
  locale: string;
  dLocale: string;
  zone: string;
  businessLanguage: Locale;
  t: ReturnType<typeof useTranslations>;
  banner: { tone: "note" | "error"; text: string } | null;
  busy: boolean;
  verifying: boolean;
  turnstileSiteKey: string | null;
  /** True on the done screen: the aside (Turnstile included) stays mounted — never conditionally
   * removed, so the same live widget survives into the next "Book another" cycle — just hidden. */
  hidden: boolean;
  onTurnstileLoad(): void;
  onBook(): void;
}) {
  const c = page.cancellation;
  const service = s2?.service ?? null;
  const slot = s2?.slot ?? null;
  const worker = s2?.worker ?? null;
  const workerName = !service ? null : service.workers.length === 1 ? service.workers[0].display_name : worker === "any" ? t("anyone") : (service.workers.find((w) => w.id === worker)?.display_name ?? "");
  const name = service ? localized(service.name, locale as Locale, businessLanguage) : null;
  const ownText = ownPolicyText(c.text, locale, businessLanguage);

  let whenText = <dd className={styles.unset}>{t("notChosen")}</dd>;
  let termLine: React.ReactNode;
  if (slot && service) {
    const start = new Date(slot);
    const end = new Date(start.getTime() + service.duration_minutes * 60_000);
    const w = localWhen(slot, zone, dLocale);
    const endTime = new Intl.DateTimeFormat(dLocale, { hour: "2-digit", minute: "2-digit", timeZone: zone }).format(end);
    whenText = <dd>{t("whenFormat", { day: w.date, from: w.time, to: endTime })}</dd>;
    const state = cancellationState(new Date(), start, c.free_cancellation_hours);
    if (state.kind === "before") {
      const freeW = localWhen(state.freeUntil.toISOString(), zone, dLocale);
      termLine = (
        <li className={styles.termsFree}>
          <CheckIcon />
          <span>
            {t("freeUntil", { when: `${freeW.date}, ${freeW.time}` })} {t("afterFree")}
          </span>
        </li>
      );
    } else {
      termLine = (
        <li>
          <AlertIcon />
          <span>{t("lateWindow", { n: c.free_cancellation_hours })}</span>
        </li>
      );
    }
  } else {
    termLine = (
      <li className={styles.termsFree}>
        <CheckIcon />
        <span>
          {t("freeHours", { n: c.free_cancellation_hours })} {t("afterFree")}
        </span>
      </li>
    );
  }

  const label = busy ? (page.auto_confirm ? t("booking") : t("sending")) : page.auto_confirm ? t("book") : t("request");

  return (
    <aside className={styles.checkout} aria-labelledby="checkout-h" hidden={hidden}>
      <h2 id="checkout-h" className={styles.checkoutHead}>
        {t("yourBooking")}
      </h2>
      <dl className={styles.summary}>
        <div className={styles.summaryRow}>
          <dt>{t("service")}</dt>
          {service && name ? <dd lang={name.lang ?? undefined}>{name.text}</dd> : <dd className={styles.unset}>{t("notChosen")}</dd>}
        </div>
        <div className={styles.summaryRow}>
          <dt>{t("when")}</dt>
          {whenText}
        </div>
        <div className={styles.summaryRow}>
          <dt>{t("withRow")}</dt>
          {workerName !== null ? <dd>{workerName}</dd> : <dd className={styles.unset}>{t("notChosen")}</dd>}
        </div>
        <div className={styles.summaryRow}>
          <dt>{t("price")}</dt>
          {service ? <dd>{formatMoney(service.price.amount_minor, service.price.currency, locale)}</dd> : <dd className={styles.unset}>{t("notChosen")}</dd>}
        </div>
      </dl>
      <section className={styles.terms} aria-labelledby="terms-h">
        <h3 id="terms-h">{t("termsTitle")}</h3>
        <ul className={styles.termsList}>
          {termLine}
          <li>
            <MoveIcon />
            <span>{c.max_reschedules > 0 ? t("moveTerms", { n: c.reschedule_cutoff_hours, m: c.max_reschedules }) : t("moveOff")}</span>
          </li>
        </ul>
        {ownText && (
          <blockquote className={styles.quote} lang={businessLanguage}>
            <p>{ownText}</p>
            <footer lang={locale}>{t("ownWords", { biz: page.name })}</footer>
          </blockquote>
        )}
      </section>
      {!page.auto_confirm && <Banner tone="note">{t("approvalNote", { biz: page.name })}</Banner>}
      {verifying && <Banner tone="note">{t("verifying")}</Banner>}
      {banner && <Banner tone={banner.tone}>{banner.text}</Banner>}
      {/* Rendered visibly (never display:none) and kept mounted for the whole page's life — an
       * interaction-only challenge must be able to actually appear here, and "Book another" must
       * reuse the same live widget rather than a dead one from a screen that unmounted it. */}
      {turnstileSiteKey && (
        <>
          <div id="turnstile-container" className={styles.turnstileBox} />
          {/* onReady, not onLoad: next/script's LoadCache means onLoad only fires the very first
           * time this script URL is ever loaded on the page — a client-side locale switch or a
           * soft nav to another slug remounts BookingPage without the browser reloading the
           * script, so onLoad would never fire again and no widget would ever get rendered.
           * onReady fires on every mount, script-cached or not. */}
          <Script src="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit" strategy="afterInteractive" onReady={onTurnstileLoad} />
        </>
      )}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          onBook();
        }}
      >
        <Submit busy={busy} busyLabel={label}>
          {label}
        </Submit>
      </form>
      <p className={styles.fine}>{t("fine", { biz: page.name })}</p>
    </aside>
  );
}

function DoneScreen({
  page,
  done,
  locale,
  dLocale,
  zone,
  t,
  onAgain,
}: {
  page: BookingPageOut;
  done: Done;
  locale: Locale;
  dLocale: string;
  zone: string;
  t: ReturnType<typeof useTranslations>;
  onAgain(): void;
}) {
  const pending = done.status === "pending";
  const start = new Date(done.starts_at);
  const end = new Date(start.getTime() + done.service.duration_minutes * 60_000);
  const w = localWhen(done.starts_at, zone, dLocale);
  const endTime = new Intl.DateTimeFormat(dLocale, { hour: "2-digit", minute: "2-digit", timeZone: zone }).format(end);
  const name = localized(done.service.name, locale, page.language as Locale);
  const lede = pending ? t("pendLede", { biz: page.name, email: done.email }) : t("okLede", { email: done.email });
  return (
    <div className={styles.outcomeGrid}>
      <Outcome icon={pending ? "mail" : "done"} title={pending ? t("pendTitle") : t("okTitle")} lede={lede}>
        <ul className={styles.doneList}>
          <li lang={name.lang ?? undefined}>{name.text}</li>
          <li>
            {t("whenFormat", { day: w.date, from: w.time, to: endTime })} {"("}
            {zoneCity(zone)}
            {")"}
          </li>
          <li>
            {done.worker?.display_name ?? ""} · {formatMoney(done.price.amount_minor, done.price.currency, locale)}
          </li>
        </ul>
        <p className={styles.hint}>{t("spam")}</p>
        <button className={`${styles.button} ${styles.secondary}`} type="button" onClick={onAgain}>
          {t("another")}
        </button>
      </Outcome>
    </div>
  );
}
