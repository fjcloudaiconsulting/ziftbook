"use client";

import Script from "next/script";
import { useTranslations } from "next-intl";
import { useEffect, useMemo, useRef, useState } from "react";

import { availabilityRead, bookingsCreate, type BookingPageOut, sessionRead } from "@/api-client";
import { Link, useRouter } from "@/i18n/navigation";
import { addDays, localDay, localWhen, slotsByDay, weekDays } from "@/lib/booking-link";
import {
  afterPickTaken,
  type AnswerOutcome,
  answerState,
  bookingBody,
  checkoutUrl,
  cleanMessage,
  emailSuggestion,
  firstFreeDayFrom,
  keepPick,
  nextWeekDisabled,
  phoneValue,
  pickFromUrl,
  scanWindow,
  slotsFor,
  slotWho,
  weekFor,
  withName,
} from "@/lib/booking-page";
import { dateLocale } from "@/lib/console";
import { defaultDial, dialOptions } from "@/lib/dial-codes";
import { type Locale } from "@/lib/services";
import { zoneCity } from "@/lib/week";

import { Banner, Chevron, FieldError, send, Submit } from "../_ui/parts";
import { CheckIcon } from "../_ui/picker";
import styles from "../_ui/ui.module.css";
import { type Done, DoneScreen, Picker, WhoAt } from "./checkout";
import { BookingFooter } from "./footer";
import { PinIcon } from "./icons";
import { type CacheEntry, type Flow, type Service, type Step2Plus, type TimesWindow } from "./shared";
import { Summary } from "./summary";

const TURNSTILE_LANG: Record<string, string> = { pt: "pt-br" };

const INITIAL: Omit<Flow, "name" | "email" | "phone" | "service" | "worker" | "weekStart"> = {
  step: 2,
  day: null,
  slot: null,
  pick: null,
  free: [],
  taken: new Set(),
  errors: {},
  suggest: null,
  banner: null,
  busy: false,
};

function NewTab({ href, children }: { href: string; children: React.ReactNode }) {
  const t = useTranslations("BookingPage");
  return (
    <Link href={href} target="_blank">
      {children}
      <span className={styles.srOnly}> {t("newTab")}</span>
    </Link>
  );
}

/** `/[slug]/book`: the day and time, then the details, then a direct booking (ZIF-157). The service,
 * the person and the time live in the URL (checkoutUrl) so a refresh restores them; nothing the
 * booker types does. */
export function BookingPage({
  page,
  locale,
  turnstileSiteKey,
  serviceId,
  memberId,
  at,
}: {
  page: BookingPageOut;
  locale: string;
  turnstileSiteKey: string | null;
  serviceId: string;
  memberId: string | null;
  at: string | null;
}) {
  const t = useTranslations("BookingPage");
  const router = useRouter();
  const loc = locale as Locale;
  const zone = page.timezone;
  const dLocale = dateLocale(locale);

  const [flow, setFlow] = useState<Flow>(() => {
    const service = page.services.find((svc) => svc.id === serviceId) ?? null;
    const today = localDay(new Date(), zone);
    const worker = memberId ?? (service?.workers.length === 1 ? service.workers[0].id : "any");
    // The week of the picked time (jumpTo's rule), so a pick weeks out comes back on screen.
    const weekStart = at ? weekFor(localDay(at, zone), today) : today;
    return { ...INITIAL, name: "", email: "", phone: "", service, worker, weekStart };
  });
  const dials = useMemo(() => dialOptions(page.country, locale), [page.country, locale]);
  const [done, setDone] = useState<Done | null>(null);
  const [region, setRegion] = useState<string>(page.country);
  const [marketing, setMarketing] = useState(false);
  const [message, setMessage] = useState("");
  const [messageOpen, setMessageOpen] = useState(false);
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
  const phoneRef = useRef<HTMLInputElement>(null);
  const messageRef = useRef<HTMLTextAreaElement>(null);
  const headingRefs = useRef<Record<number, HTMLHeadingElement | null>>({});
  const [focusStep, setFocusStep] = useState<number | null>(null);
  // Paired with a generation counter, not just the field name: pressing Send twice with the SAME
  // field still invalid both times must still re-focus it — a plain "name"/"name" value wouldn't
  // change, so the effect below wouldn't re-fire.
  const [focusField, setFocusField] = useState<{ field: "name" | "email" | "phone" | "message"; gen: number } | null>(null);
  const focusFieldGen = useRef(0);
  const [verifying, setVerifying] = useState(false);
  const [turnstileToken, setTurnstileToken] = useState<string | null>(null);
  const turnstileWidget = useRef<string | null>(null);
  const turnstileTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingSubmit = useRef(false);
  const [firstFreeByEntry, setFirstFreeByEntry] = useState(new Map<string, string>());
  // Always current, read from the Turnstile widget's own callback: that callback is created once
  // by onTurnstileLoad and would otherwise close over the render's doBook from that moment (stale).
  const doBookRef = useRef<(token: string | null) => Promise<void>>(async () => {});

  // Autofill: GET /api/session once (the person's own name, not their name in some business); fill
  // name/email ONLY if still empty. Any other answer (no session, network, whatever) is ignored
  // silently.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const answer = await send(sessionRead());
      if (cancelled || answer.status !== 200 || !answer.data) return;
      setFlow((f) => ({ ...f, name: f.name || answer.data!.name || f.name, email: f.email || answer.data!.email || f.email }));
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Once mounted, a tick later (never a state update in the effect's own body): the week's times
  // load, and a time from the URL is kept only if that window still offers it (else `at` is dropped).
  useEffect(() => {
    if (!flow.service) return;
    const service = flow.service;
    const timer = setTimeout(
      () =>
        void (async () => {
          const win = await ensureWeek(service, flow.worker, flow.weekStart);
          const slot = win ? pickFromUrl(at, slotsFor(win, flow.worker)) : null;
          if (win && slot) {
            const free = flow.worker === "any" ? (win.who[win.slots.indexOf(slot)] ?? []) : [];
            // Only while the client has not changed worker or picked a time meanwhile.
            setFlow((f) => (f.worker === flow.worker && !f.slot ? { ...f, slot, free, day: localDay(slot, zone), step: 3 } : f));
          } else if (win && at) {
            // Drop only `at` from the address as it is now: a worker picked meanwhile already wrote `with`.
            const url = new URL(window.location.href);
            url.searchParams.delete("at");
            window.history.replaceState(null, "", url);
          }
        })(),
    );
    return () => clearTimeout(timer);
    // Mount only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (focusStep !== null) headingRefs.current[focusStep]?.focus();
  }, [focusStep]);

  // Runs after the details have (re)rendered with their inputs mounted, so a field targeted before
  // they were open still gets focused once they are.
  useEffect(() => {
    if (focusField?.field === "name") nameRef.current?.focus();
    else if (focusField?.field === "email") emailRef.current?.focus();
    else if (focusField?.field === "phone") phoneRef.current?.focus();
    else if (focusField?.field === "message") messageRef.current?.focus();
  }, [focusField, flow.step]);

  // One window for everyone (ZIF-100): a person's times are filtered out of it locally (slotsFor).
  function cacheKey(service: string, from: string) {
    return `${service}|${from}`;
  }

  /** The service is gone (a 404 on the times or on Send): back to the list, which says so. */
  function goneToList() {
    router.replace(`/${page.slug}?gone=1`);
  }

  /** The address keeps the service, the person and the time (checkoutUrl); Back still returns to the list. */
  function syncUrl(worker: string, slot: string | null) {
    // Native History, not the router: same route, so no scroll to top and no server round trip (Next syncs useSearchParams).
    window.history.replaceState(null, "", `/${locale}${checkoutUrl(page.slug, { serviceId, memberId: worker === "any" ? null : worker, at: slot })}`);
  }

  /** The 14-day window starting at `from`, cached so paging within it needs no new fetch. Never
   * resolves to "loading": that value only ever marks an in-flight fetch in the cache map itself.
   * `bypass`: skip a cached entry and fetch fresh (after a 409, the cached window is stale). */
  async function loadWindow(service: Service, from: string, bypass = false): Promise<TimesWindow | "error" | "gone"> {
    const key = cacheKey(service.id, from);
    if (!bypass) {
      const existing = cache.get(key);
      if (existing && existing !== "loading") return existing;
    }
    setCache((m) => new Map(m).set(key, "loading"));
    const { from: qFrom, to } = scanWindow(from);
    const answer = await send(
      availabilityRead({ path: { tenant_id: page.id, service_id: service.id }, query: { from: qFrom, to } }),
    );
    if (answer.status === 200 && answer.data) {
      const entry: TimesWindow = { slots: answer.data.slots, who: slotWho(answer.data.slot_workers, answer.data.workers) };
      setCache((m) => new Map(m).set(key, entry));
      return entry;
    }
    if (answer.status === 404) {
      // The service (or every worker on it) is gone: same path as a 404 on Send.
      goneToList();
      return "gone";
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

  /** Loads the week's window (and scans on when the week is empty); resolves to that first window. */
  async function ensureWeek(service: Service, worker: string, weekStart: string, bypass = false): Promise<TimesWindow | null> {
    const entryKey = `${service.id}|${worker}`;
    const myToken = (ensureWeekToken.current.get(entryKey) ?? 0) + 1;
    ensureWeekToken.current.set(entryKey, myToken);
    const isCurrent = () => ensureWeekToken.current.get(entryKey) === myToken;

    const windowFrom = windowFromFor(weekStart, weekStart);
    const entry = await loadWindow(service, windowFrom, bypass);
    if (typeof entry === "string") return null;
    if (!isCurrent()) return entry;
    const mine = slotsFor(entry, worker);

    const visibleWeekEmpty = weekDays(weekStart).every((d) => !(slotsByDay(mine, zone).get(d) ?? []).length);
    if (!visibleWeekEmpty) return entry;

    // B8: what's already in hand (this SAME 14-day fetch covers the following week too) is
    // checked before any further request — a free day may already be sitting in this window.
    let first = firstFreeDayFrom(mine, weekStart, zone);
    if (first === null && !scanned.current.has(entryKey)) {
      let from = windowFrom;
      const businessToday = localDay(new Date(), zone);
      for (let i = 0; i < 5; i++) {
        from = addDays(from, 14);
        if (nextWeekDisabled(businessToday, from, page.booking_horizon_days)) break;
        const next = await loadWindow(service, from);
        // A stale scan (superseded while awaiting) never marks the key scanned: an incomplete
        // scan isn't a real "nothing found" result, and a later call must be free to redo it.
        if (typeof next === "string" || !isCurrent()) return entry;
        first = firstFreeDayFrom(slotsFor(next, worker), from, zone);
        if (first !== null) break;
      }
      // Reached here only by completing the scan (found something, hit the horizon, or the cap) —
      // never by bailing out early — so it's now safe to skip a repeat for this service/worker.
      scanned.current.add(entryKey);
    }
    if (!isCurrent()) return entry; // superseded by a newer call for this service/worker while awaiting
    setFirstFreeByEntry((m) => new Map(m).set(entryKey, first ?? ""));
    return entry;
  }

  /** Keeps the day (PickerWeek falls back to the first day with times when this person has none
   * that day); clears the time, which this person may not be free at. */
  function chooseWorker(worker: string) {
    setFlow((f) => ({ ...f, worker, slot: null, pick: null, banner: f.banner?.where === 3 ? null : f.banner }));
    const f = flow as Step2Plus;
    void ensureWeek(f.service, worker, f.weekStart);
    syncUrl(worker, null);
  }

  function changeWeek(delta: number) {
    setFlow((f) => ({ ...f, weekStart: addDays(f.weekStart, delta), day: null }));
    const f = flow as Step2Plus;
    void ensureWeek(f.service, f.worker, addDays(f.weekStart, delta));
  }

  function jumpTo(day: string) {
    const weekStart = weekFor(day, localDay(new Date(), zone));
    setFlow((f) => ({ ...f, weekStart, day }));
    const f = flow as Step2Plus;
    void ensureWeek(f.service, f.worker, weekStart);
  }

  function pickSlot(slot: string, free: string[]) {
    setFlow((f) => ({ ...f, slot, free, pick: keepPick(f.pick, f.service!.workers, free), banner: f.banner?.where === 2 || f.banner?.where === 3 ? null : f.banner }));
    syncUrl(flow.worker, slot);
  }

  /** Continue: the details open below, or the missing time is named at the picker. */
  function next() {
    if (!flow.slot) {
      setFlow((f) => ({ ...f, banner: { where: 2, tone: "error", text: t("errMissingTime") } }));
      return;
    }
    if (flow.step === 3) headingRefs.current[3]?.focus();
    else {
      setFlow((f) => ({ ...f, step: 3 }));
      setFocusStep(3);
    }
  }

  function fieldChange(field: "name" | "email" | "phone", value: string) {
    setFlow((f) => {
      const next = { ...f, [field]: value };
      if (f.errors[field]) next.errors = { ...f.errors, [field]: undefined };
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
  // warning that follows from just letting its container be torn out from under it. Clearing the
  // ref after also means a StrictMode remount's mount-time check below never mistakes it for a
  // still-live widget.
  useEffect(() => {
    return () => {
      const w = (window as unknown as { turnstile?: { remove(id: string): void } }).turnstile;
      if (w?.remove && turnstileWidget.current) w.remove(turnstileWidget.current);
      turnstileWidget.current = null;
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

  // The widget's box only exists once the details are open (and stays for the page's life), so it
  // renders then if the script is already there; otherwise the script's onReady does. It also covers
  // StrictMode's mount/unmount/remount, whose second mount has no widget of its own yet.
  useEffect(() => {
    const w = (window as unknown as { turnstile?: unknown }).turnstile;
    if (flow.step === 3 && turnstileSiteKey && w && !turnstileWidget.current) onTurnstileLoad();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [flow.step]);

  function resetTurnstile() {
    setTurnstileToken(null);
    const w = (window as unknown as { turnstile?: { reset(id?: string): void } }).turnstile;
    if (w && turnstileWidget.current) w.reset(turnstileWidget.current);
  }

  /** Send: every invalid field is marked and the first focused; a missing time is named at the picker. */
  async function book() {
    if (working.current) return;
    const errors: Flow["errors"] = {};
    if (!flow.name.trim()) errors.name = t("errName");
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(flow.email.trim())) errors.email = t("errEmail");
    if (!flow.phone.trim()) errors.phone = t("errPhone");
    const missingTime = !flow.slot;
    if (missingTime || Object.keys(errors).length > 0) {
      setFlow((f) => ({ ...f, errors, banner: missingTime ? { where: 2, tone: "error", text: t("errMissingTime") } : f.banner }));
      const first = errors.name ? "name" : errors.email ? "email" : errors.phone ? "phone" : null;
      if (first) {
        focusFieldGen.current += 1;
        setFocusField({ field: first, gen: focusFieldGen.current });
      } else headingRefs.current[2]?.focus();
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
    const member = f.pick ?? (f.worker === "any" ? null : f.worker);
    const body = bookingBody({
      startsAt: f.slot!,
      memberId: member,
      name: f.name,
      email: f.email,
      phone: phoneValue(defaultDial(region), defaultDial(page.country), f.phone),
      locale,
      policyVersion: page.consent[loc].policy_version,
      turnstileToken: token,
      consent: marketing,
      message: cleanMessage(message),
    });
    const answer = await send(bookingsCreate({ path: { tenant_id: page.id, service_id: f.service.id }, body: { ...body, locale: loc } }));
    working.current = false;
    const outcome = answerState(answer, Boolean(turnstileSiteKey));
    // Reset after EVERY answer, 201 included: a token is never reused.
    if (turnstileSiteKey) resetTurnstile();

    if (outcome.kind === "done" && answer.data) {
      setDone({
        status: answer.data.status,
        service: f.service,
        worker: f.worker === "any" ? (f.service.workers.find((w) => w.id === answer.data!.worker_id) ?? null) : (f.service.workers.find((w) => w.id === f.worker) ?? null),
        starts_at: answer.data.starts_at,
        price: answer.data.price,
        email: f.email,
      });
      syncUrl(f.worker, null);
      return;
    }
    await afterRefusal(f, outcome);
  }

  function failureText(kind: AnswerOutcome["kind"]): string {
    return kind === "verifyFailed"
      ? t("verifyFailed")
      : kind === "unavailable"
        ? t("bookingUnavailable")
        : kind === "tooMany"
          ? t("tooMany")
          : kind === "nothingBooked"
            ? t("nothingBooked")
            : kind === "fieldErrors"
              ? t("checkDetails")
              : t("unknownOutcome");
  }

  /** Every refused POST: the time taken, the service gone, the terms changed, or a banner by the button. */
  async function afterRefusal(f: Step2Plus, outcome: AnswerOutcome) {
    if (outcome.kind === "slotTaken") {
      const time = localWhen(f.slot!, zone, dateLocale(locale)).time;
      // The window holding the slot, not the visible week's: "Change" plus "Next week" keeps the slot.
      const slotWindow = windowFromFor(f.weekStart, localDay(f.slot!, zone));
      let fresh: TimesWindow | "error" | "gone" = "error";
      if (f.pick) {
        // A person picked at that time was taken: if someone else is still free then, keep the
        // time and drop the pick (decision 5); nothing is booked until Book is pressed again.
        // Still working: a second Book press must not POST the stale pick while this refetches.
        working.current = true;
        fresh = await loadWindow(f.service, slotWindow, true);
        working.current = false;
        if (fresh === "gone") return; // loadWindow already sent the client back to step 1
        const at = fresh === "error" ? -1 : fresh.slots.indexOf(f.slot!);
        const rest = afterPickTaken(fresh === "error" || at < 0 ? null : fresh.who[at], f.pick);
        if (rest) {
          const name = f.service.workers.find((w) => w.id === f.pick)?.display_name ?? "";
          // Only onto the same time: one picked meanwhile (from step 2, reopened) is left alone.
          setFlow((cur) => (cur.slot !== f.slot ? { ...cur, busy: false } : { ...cur, step: 3, pick: null, free: rest, busy: false, banner: { where: 3, tone: "note", text: t("pickTaken", { name, time }) } }));
          // Not setFocusStep(3): it is usually 3 already (from picking the time), so it wouldn't re-fire.
          headingRefs.current[3]?.focus();
          return;
        }
      }
      // Functional: a pick's refetch above awaited, and anything typed meanwhile must survive.
      setFlow((cur) => ({
        ...cur,
        taken: new Set([...cur.taken, f.slot!]),
        busy: false,
        ...(cur.slot === f.slot ? { slot: null, pick: null, banner: { where: 2 as const, tone: "note" as const, text: t("slotTaken", { time }) } } : {}),
      }));
      setFocusStep(2);
      // The cached window is stale (it still offers the just-taken slot): bypass it, unless the
      // pick's refetch above just refreshed it.
      void ensureWeek(f.service, f.worker, f.weekStart, fresh === "error" || slotWindow !== f.weekStart);
      return;
    }
    if (outcome.kind === "serviceGone") {
      goneToList();
      return;
    }
    if (outcome.kind === "policyChanged") {
      router.refresh();
      setFlow({ ...f, busy: false, banner: { where: "c", tone: "error", text: t("policyChanged") } });
      return;
    }
    if (outcome.kind === "phoneInvalid") {
      // ZIF-152: under the field, never the banner.
      setFlow({ ...f, busy: false, banner: null, errors: { ...f.errors, phone: t("errPhoneInvalid") } });
      focusFieldGen.current += 1;
      setFocusField({ field: "phone", gen: focusFieldGen.current });
      return;
    }
    if (outcome.kind === "messageInvalid") {
      // B1: under the message, which opens if it was folded; never the generic banner.
      setFlow({ ...f, busy: false, banner: null, errors: { ...f.errors, message: t("errMessage") } });
      setMessageOpen(true);
      focusFieldGen.current += 1;
      setFocusField({ field: "message", gen: focusFieldGen.current });
      return;
    }
    setFlow({ ...f, busy: false, banner: { where: "c", tone: "error", text: failureText(outcome.kind) } });
  }

  useEffect(() => {
    doBookRef.current = doBook;
  });

  const s2 = flow.service ? (flow as Step2Plus) : null;
  if (!s2) return null;
  const s2With = withName(s2.service.workers, s2.worker, s2.pick, t("anyone"));
  const detailsOpen = flow.step === 3;
  const auto = page.auto_confirm;
  const sendLabel = flow.busy ? (auto ? t("booking") : t("sending")) : auto ? t("book") : t("request");
  if (done) {
    return (
      <>
        <main className={styles.screen}>
          <div className={`${styles.col} ${styles.bookingWide}`}>
            <DoneScreen page={page} done={done} locale={loc} dLocale={dLocale} zone={zone} t={t} onAgain={() => router.push(`/${page.slug}`)} />
          </div>
        </main>
        <BookingFooter />
      </>
    );
  }

  return (
    <>
      <main className={styles.screen}>
        <div className={`${styles.col} ${styles.bookingWide}`}>
          <Link className={styles.back} href={`/${page.slug}`}>
            <svg aria-hidden="true" viewBox="0 0 12 12" width="12" height="12">
              <path d="M7.5 2.5l-3 3.5 3 3.5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            {t("backToServices")}
          </Link>
          <div className={styles.bizHeader}>
            <h1>{t("checkoutTitle")}</h1>
            <p className={styles.bizMeta}>
              <PinIcon />
              <span>{page.name}</span>
              <span aria-hidden="true">·</span>
              <span>{t("timesIn", { city: zoneCity(zone) })}</span>
            </p>
          </div>

          <div className={styles.bookingLayout}>
            <div className={styles.flowCol}>
              {/* Step 1: person and time. */}
              <section className={styles.step} aria-labelledby="step2-h">
                <div className={styles.stepHead}>
                  <span className={`${styles.stepNum} ${detailsOpen ? styles.stepNumDone : styles.stepNumOpen}`} aria-hidden="true">
                    {detailsOpen && s2.slot ? <CheckIcon /> : 1}
                  </span>
                  <h2 id="step2-h" ref={(el) => { headingRefs.current[2] = el; }} tabIndex={-1}>
                    {t("step2")}
                  </h2>
                </div>
                <div className={styles.stepBody}>
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
                    onDay={(d) => setFlow((f) => ({ ...f, day: d }))}
                    onSlot={pickSlot}
                    onJump={jumpTo}
                    onAnyone={() => chooseWorker("any")}
                    onRetry={() => void ensureWeek(s2.service, s2.worker, s2.weekStart, true)}
                  />
                  <div className={styles.actions}>
                    <button className={`${styles.button} ${styles.primary}`} type="button" onClick={next}>
                      {t("continue")}
                    </button>
                  </div>
                </div>
              </section>

              {/* Step 2: details, opened by Continue. */}
              <section className={`${styles.step} ${detailsOpen ? "" : styles.stepLater}`} aria-labelledby="step3-h">
                <div className={styles.stepHead}>
                  <span className={`${styles.stepNum} ${detailsOpen ? styles.stepNumOpen : ""}`} aria-hidden="true">
                    {2}
                  </span>
                  <h2 id="step3-h" ref={(el) => { headingRefs.current[3] = el; }} tabIndex={-1}>
                    {t("step3")}
                  </h2>
                </div>
                {detailsOpen && (
                  <div className={styles.stepBody}>
                    <WhoAt s2={s2} banner={flow.banner?.where === 3 ? flow.banner : null} zone={zone} dLocale={dLocale} t={t} onPick={(pick) => setFlow((f) => ({ ...f, pick }))} />
                    <form
                      className={styles.form}
                      method="post"
                      noValidate
                      onSubmit={(event) => {
                        event.preventDefault();
                        void book();
                      }}
                    >
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
                      <div className={styles.field}>
                        <label className={styles.label} htmlFor="bp-phone">
                          {t("phone")}
                        </label>
                        <div className={styles.phonePair}>
                          <div className={styles.input}>
                            <select id="bp-dial" name="dial" aria-label={t("dialCode")} value={region} onChange={(e) => setRegion(e.target.value)}>
                              {dials.map((o) => (
                                <option key={o.region} value={o.region}>
                                  {o.text}
                                </option>
                              ))}
                            </select>
                            <Chevron select />
                          </div>
                          <div className={styles.input}>
                            <input
                              ref={phoneRef}
                              id="bp-phone"
                              name="phone"
                              type="tel"
                              autoComplete="tel-national"
                              maxLength={36}
                              value={flow.phone}
                              onChange={(e) => fieldChange("phone", e.target.value)}
                              aria-invalid={flow.errors.phone ? true : undefined}
                              aria-describedby={flow.errors.phone ? "bp-phone-hint bp-phone-err" : "bp-phone-hint"}
                            />
                          </div>
                        </div>
                        <p className={styles.hint} id="bp-phone-hint">
                          {t("phoneUse", { biz: page.name })}
                        </p>
                        {flow.errors.phone && <FieldError id="bp-phone-err">{flow.errors.phone}</FieldError>}
                      </div>
                      <p className={styles.fine}>
                        {t.rich("legal", { biz: page.name, terms: (chunks) => <NewTab href="/terms">{chunks}</NewTab>, privacy: (chunks) => <NewTab href="/privacy">{chunks}</NewTab>, rules: (chunks) => <a href="#cancel-terms">{chunks}</a> })}
                      </p>
                      <label className={styles.consentRow}>
                        <input type="checkbox" checked={marketing} onChange={(e) => setMarketing(e.target.checked)} />
                        <span>{page.consent[loc].marketing_email}</span>
                      </label>
                      {/* Rendered visibly (never display:none) and kept mounted once the details are
                       * open: an interaction-only challenge must be able to actually appear here. */}
                      {turnstileSiteKey && <div id="turnstile-container" className={styles.turnstileBox} />}
                      {verifying && <Banner tone="note">{t("verifying")}</Banner>}
                      {flow.banner?.where === "c" && <Banner tone={flow.banner.tone}>{flow.banner.text}</Banner>}
                      <Submit busy={flow.busy} busyLabel={sendLabel}>
                        {sendLabel}
                      </Submit>
                    </form>
                  </div>
                )}
              </section>
            </div>

            <Summary
              page={page}
              service={s2.service}
              slot={s2.slot}
              workerName={s2With}
              locale={locale}
              dLocale={dLocale}
              zone={zone}
              t={t}
              message={message}
              messageOpen={messageOpen}
              messageError={flow.errors.message}
              messageRef={messageRef}
              onMessage={(text) => {
                setMessage(text);
                if (flow.errors.message) setFlow((f) => ({ ...f, errors: { ...f.errors, message: undefined } }));
              }}
              onMessageOpen={setMessageOpen}
            />
          </div>
        </div>
      </main>
      <BookingFooter />
      {/* onReady, not onLoad: next/script's LoadCache means onLoad only fires the very first time this
       * script URL is ever loaded on the page — a client-side locale switch or soft nav remounts this
       * page without the browser reloading the script, so onLoad would never fire again and no
       * widget would ever get rendered. onReady fires on every mount, script-cached or not. */}
      {turnstileSiteKey && <Script src="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit" strategy="afterInteractive" onReady={onTurnstileLoad} />}
    </>
  );
}
