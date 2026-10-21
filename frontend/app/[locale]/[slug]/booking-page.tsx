"use client";

import Script from "next/script";
import { useTranslations } from "next-intl";
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";

import { availabilityRead, bookingsAbandon, bookingsCreate, bookingsResume, bookingsSync, type BookingPageOut, type PaymentStart, sessionRead, type SyncOut } from "@/api-client";
import { Link, useRouter } from "@/i18n/navigation";
import { addDays, localDay, localWhen, slotsByDay, weekDays } from "@/lib/booking-link";
import {
  afterPickTaken,
  type AnswerOutcome,
  answerState,
  bookingBody,
  checkoutUrl,
  cleanMessage,
  doneState,
  emailSuggestion,
  entryMode,
  firstFreeDayFrom,
  keepPick,
  mayForget,
  nextWeekDisabled,
  type PaymentChoice,
  phoneValue,
  pickFromUrl,
  returnParams,
  scanWindow,
  sendAction,
  sendLabel,
  slotsFor,
  slotWho,
  storedNext,
  termsLines,
  weekFor,
  withName,
} from "@/lib/booking-page";
import { dateLocale } from "@/lib/console";
import { defaultDial, dialOptions } from "@/lib/dial-codes";
import { formatMoney } from "@/lib/money";
import { type Locale } from "@/lib/services";
import type { AppVersion } from "@/lib/version";
import { zoneCity } from "@/lib/week";

import { Banner, FieldError, JSON_WRITE, send, Submit } from "../_ui/parts";
import { CheckIcon } from "../_ui/picker";
import styles from "../_ui/ui.module.css";
import { type Done, DoneScreen, PayEnd, Picker, Processing, ResumeView, WhoAt } from "./checkout";
import { DialPicker } from "./dial-picker";
import { BookingFooter, NewTab } from "./footer";
import { PinIcon } from "./icons";
import { type PayHandle, PaymentStep } from "./payment-step";
import { type CacheEntry, type Flow, localized, type Service, type Step2Plus, type TimesWindow } from "./shared";
import { Summary } from "./summary";

const TURNSTILE_LANG: Record<string, string> = { pt: "pt-br" };

// An open checkout survives leaving the page: the booking waiting for payment, for this tab only.
const STORE = "zif-checkout";
type Stored = { bookingId: string; serviceId: string; email: string | null };

function readStored(): Stored | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(STORE) ?? "null");
    return value && typeof value.bookingId === "string" && typeof value.serviceId === "string" ? value : null;
  } catch {
    return null;
  }
}

function writeStored(value: Stored | null) {
  try {
    if (value) sessionStorage.setItem(STORE, JSON.stringify(value));
    else sessionStorage.removeItem(STORE);
  } catch {
    // Private mode: the open checkout is simply not remembered.
  }
}

/** `booking` in the address from the 201 on (a refresh, Back from a bank page or Stripe's return finds it);
 * null takes it out, with the picked time too when `dropAt` (that time is booked, or gone). */
function setBookingParam(id: string | null, dropAt = false) {
  const url = new URL(window.location.href);
  if (id) url.searchParams.set("booking", id);
  else url.searchParams.delete("booking");
  if (dropAt) url.searchParams.delete("at");
  window.history.replaceState(null, "", url);
}

type End = "processing" | "expired" | "expiredPaid" | "expiredProcessing" | "stale" | "checkEmail";

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

/** `/[slug]/book`: the day and time, then the details, then a direct booking (ZIF-157). The service,
 * the person and the time live in the URL (checkoutUrl) so a refresh restores them; nothing the
 * booker types does. */
export function BookingPage({
  page,
  locale,
  turnstileSiteKey,
  version,
  serviceId,
  memberId,
  at,
  booking,
}: {
  page: BookingPageOut;
  locale: string;
  turnstileSiteKey: string | null;
  version: AppVersion | null;
  serviceId: string;
  memberId: string | null;
  at: string | null;
  /** A booking waiting for its payment, from the address. */
  booking: string | null;
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
  const focusOn = (field: "name" | "email" | "phone" | "message") => {
    focusFieldGen.current += 1;
    setFocusField({ field, gen: focusFieldGen.current });
  };
  const [verifying, setVerifying] = useState(false);
  const [turnstileToken, setTurnstileToken] = useState<string | null>(null);
  const turnstileWidget = useRef<string | null>(null);
  const turnstileTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingSubmit = useRef(false);
  const [firstFreeByEntry, setFirstFreeByEntry] = useState(new Map<string, string>());
  // Always current, read from the Turnstile widget's own callback: that callback is created once
  // by onTurnstileLoad and would otherwise close over the render's doBook from that moment (stale).
  const doBookRef = useRef<(token: string | null) => Promise<void>>(async () => {});

  // --- ZIF-158: the payment step ---
  const liveService = page.services.find((svc) => svc.id === serviceId);
  const stepOn = Boolean(page.payment && liveService?.payment);
  const [choice, setChoice] = useState<PaymentChoice>("check");
  const [bookingId, setBookingId] = useState<string | null>(booking);
  const [checking, setChecking] = useState(booking !== null);
  const [start, setStart] = useState<PaymentStart | null>(null);
  const [sync, setSync] = useState<SyncOut | null>(null);
  const [end, setEnd] = useState<End | null>(null);
  const [offer, setOffer] = useState<SyncOut | null>(null);
  const [payGone, setPayGone] = useState(false);
  const [recheckFailed, setRecheckFailed] = useState(false);
  const stepRef = useRef<PayHandle>(null);
  // The request is sent and waits for payment: its fields stay as they were (a retry must never confirm
  // the old intent for a changed time or address).
  const locked = bookingId !== null && sync === null && !checking;

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
    // A booking in the address is resumed (below), never re-picked: its own time is occupied by it.
    if (!flow.service || booking) return;
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
            // Drop only this stale `at`: a worker or time picked meanwhile already wrote the address.
            const url = new URL(window.location.href);
            if (url.searchParams.get("at") === at) {
              url.searchParams.delete("at");
              window.history.replaceState(null, "", url);
            }
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
    if (focusField) ({ name: nameRef, email: emailRef, phone: phoneRef, message: messageRef })[focusField.field].current?.focus();
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
        // Only a send waiting on the verification owns the lock; a re-challenge during a confirm does not.
        if (pendingSubmit.current) working.current = false;
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

  /** The stored checkout is still waiting: pick it up again (the resume view). */
  function resumeOffer() {
    if (!offer || working.current) return; // a Send waiting on Turnstile owns the page
    setBookingParam(offer.id);
    apply(offer, storedEmail());
  }

  async function startOver() {
    if (working.current) return;
    if (offer && (await release(offer.id, storedEmail(), 2))) {
      writeStored(null);
      setOffer(null);
    }
  }

  const returnUrl = (id: string) => `${window.location.origin}/${locale}/${page.slug}/book?${new URLSearchParams({ service: serviceId, booking: id })}`;
  const storedEmail = () => readStored()?.email ?? null;

  /** An error by the button; the request stays as it is. */
  function fail(text: string, tone: "error" | "note" = "error", where: 2 | "c" = "c") {
    working.current = false;
    setFlow((f) => ({ ...f, busy: false, banner: { where, tone, text } }));
  }

  function forget(dropAt: boolean) {
    writeStored(null);
    setBookingParam(null, dropAt);
  }

  const checkBooking = (id: string) => send(bookingsSync({ ...JSON_WRITE, path: { tenant_id: page.id, booking_id: id } }));

  /** The one place that decides what a booking's payment state shows (doneState). */
  function apply(s: SyncOut, email: string | null) {
    const view = doneState(s);
    setSync(s);
    setBookingId(s.id);
    if (view.kind === "done") {
      forget(true);
      setEnd(null);
      setOffer(null);
      setDone({
        status: s.status,
        service: { name: s.service_name, duration_minutes: s.duration_minutes },
        worker: { display_name: s.worker_display_name },
        starts_at: s.starts_at,
        price: s.price,
        email,
        payment: s.payment.kind === "pay" ? "paid" : "checked",
      });
      return;
    }
    if (view.kind !== "resume") {
      // Whatever was in flight (a switch, a release) ends here; a resume's reopen takes it over.
      working.current = false;
      setFlow((f) => ({ ...f, busy: false }));
    }
    if (view.kind === "resume") {
      setEnd(null);
      if (view.failed) fail(t("payFailed"));
      void reopen(s.id, s.payment.kind, email);
      return;
    }
    if (view.kind !== "processing") forget(true);
    setEnd(view.kind);
  }

  /** PUT resume: the open intent's secret for this kind (a new intent when the kind changed). */
  async function reopen(id: string, kind: PaymentChoice, email: string | null) {
    // Working until the answer: Send must not confirm the old intent while the kind is switching.
    working.current = true;
    setFlow((f) => ({ ...f, busy: true }));
    const answer = await send(bookingsResume({ path: { tenant_id: page.id, booking_id: id }, body: { kind } }));
    if (answer.status === 200 && answer.data) {
      // The choice follows the intent it belongs to, never ahead of it.
      setChoice(kind);
      setStart(answer.data);
      working.current = false;
      setFlow((f) => ({ ...f, busy: false }));
    } else if (answer.status === 422 && answer.code === "payment_not_offered") {
      setPayGone(true);
      working.current = false;
      setFlow((f) => ({ ...f, busy: false }));
    } else if (answer.status === 409) {
      const again = await checkBooking(id);
      if (again.status === 200 && again.data) apply(again.data, email);
      else fail(t("payCheckEmail"), "note");
    } else if (answer.status === 429) fail(t("payCheckEmail"), "note");
    else fail(t("payUnavailable"));
  }

  /** Confirm the intent with the Element, then ask where the payment stands. `submitFirst`: the Element's
   * fields were not just checked by Send (a retry after an error). */
  async function pay(id: string, st: PaymentStart, submitFirst: boolean, email: string | null) {
    working.current = true;
    setFlow((f) => ({ ...f, busy: true, banner: null }));
    const step = stepRef.current;
    const error = !step ? t("payUnavailable") : ((submitFirst ? await step.submit() : null) ?? (await step.confirm(st, returnUrl(id))));
    if (error) return fail(error);
    const answer = await checkBooking(id);
    working.current = false;
    setFlow((f) => ({ ...f, busy: false }));
    if (answer.status === 200 && answer.data) apply(answer.data, email);
    else setEnd("checkEmail");
  }

  /** Release the held time. true: gone (or never there); false: the payment landed first (shown instead) or it failed. */
  async function release(id: string, email: string | null, where: 2 | "c" = "c"): Promise<boolean> {
    const answer = await send(bookingsAbandon({ ...JSON_WRITE, path: { tenant_id: page.id, booking_id: id } }));
    if (answer.status === 204 || answer.status === 404) return true;
    if (answer.status === 409) {
      const again = await checkBooking(id);
      if (again.status === 200 && again.data) apply(again.data, email);
      else fail(t("payCheckEmail"), "note", where);
    } else fail(t("unknownOutcome"), "error", where);
    return false;
  }

  /** Back to a plain checkout: no booking in the address, nothing stored, the form open again. */
  function reset() {
    forget(false);
    setBookingId(null);
    setStart(null);
    setSync(null);
    setEnd(null);
    setOffer(null);
    setPayGone(false);
    working.current = false;
    setFlow((f) => ({ ...f, busy: false, banner: null }));
    // A resumed page never loaded the week (the address had a booking): the picker needs it now.
    if (flow.service) void ensureWeek(flow.service, flow.worker, flow.weekStart);
  }

  async function recheck() {
    if (!bookingId || working.current) return;
    working.current = true;
    setRecheckFailed(false);
    setFlow((f) => ({ ...f, busy: true }));
    const answer = await checkBooking(bookingId);
    working.current = false;
    setFlow((f) => ({ ...f, busy: false }));
    if (answer.status === 200 && answer.data) apply(answer.data, storedEmail());
    else if (answer.status === 404 || answer.status === 422) {
      // The booking is gone: nothing left to check.
      forget(false);
      setEnd("stale");
    } else setRecheckFailed(true);
  }

  /** "Change time or details". */
  async function changeRequest() {
    if (!bookingId || working.current) return;
    working.current = true;
    setFlow((f) => ({ ...f, busy: true, banner: null }));
    // Not released: fail() or apply() already cleared the lock (or handed it to a reopen).
    if (await release(bookingId, storedEmail())) reset();
  }

  /** The radio: a sent request swaps its intent for the other kind. */
  function chooseKind(kind: PaymentChoice) {
    if (working.current) return;
    if (bookingId) void reopen(bookingId, kind, storedEmail());
    else setChoice(kind);
  }

  /** Send: every invalid field is marked and the first focused; a missing time is named at the picker. */
  /** Send on a request that exists: confirm with the secret held, or fetch one first (a resume that failed). */
  async function resend(id: string, email: string | null) {
    const action = sendAction({ working: working.current, sent: true, hasSecret: start !== null });
    if (action === "pay") await pay(id, start!, true, email);
    else if (action === "reopen") await reopen(id, sync?.payment.kind ?? choice, email);
  }

  async function book() {
    if (sendAction({ working: working.current, sent: locked, hasSecret: start !== null }) === "ignore") return;
    if (locked) return resend(bookingId!, flow.email);
    const errors: Flow["errors"] = {};
    if (!flow.name.trim()) errors.name = t("errName");
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(flow.email.trim())) errors.email = t("errEmail");
    if (!flow.phone.trim()) errors.phone = t("errPhone");
    const missingTime = !flow.slot;
    if (missingTime || Object.keys(errors).length > 0) {
      setFlow((f) => ({ ...f, errors, banner: missingTime ? { where: 2, tone: "error", text: t("errMissingTime") } : f.banner }));
      const first = errors.name ? "name" : errors.email ? "email" : errors.phone ? "phone" : null;
      if (first) {
        focusOn(first);
      } else headingRefs.current[2]?.focus();
      return;
    }
    // Working from here to the answer: a second press, or the token of an earlier verification, must not post again.
    working.current = true;
    if (stepOn) {
      // Inside the click: wallet sheets (Apple Pay, Google Pay) only open from a user gesture, which the
      // Turnstile callback is not.
      const problem = (await stepRef.current?.submit()) ?? (stepRef.current ? null : t("payUnavailable"));
      if (problem) return fail(problem);
    }
    if (turnstileSiteKey && !turnstileToken) {
      setVerifying(true);
      pendingSubmit.current = true;
      turnstileTimer.current = setTimeout(() => {
        if (pendingSubmit.current) {
          pendingSubmit.current = false;
          working.current = false;
          setVerifying(false);
          setFlow((f) => ({ ...f, banner: { where: "c", tone: "error", text: t("verifyTimeout") } }));
        }
      }, 10_000);
      return;
    }
    await doBook(turnstileToken);
  }

  async function doBook(token: string | null) {
    // The request is already sent (the form is locked): a late verification token must not post it again.
    if (locked) {
      working.current = false;
      return;
    }
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
      payment: stepOn ? choice : null,
    });
    const answer = await send(bookingsCreate({ path: { tenant_id: page.id, service_id: f.service.id }, body: { ...body, locale: loc } }));
    working.current = false;
    const outcome = answerState(answer, Boolean(turnstileSiteKey));
    // Reset after EVERY answer, 201 included: a token is never reused.
    if (turnstileSiteKey) resetTurnstile();

    if (outcome.kind === "done" && answer.data?.status === "awaiting_payment" && answer.data.payment) {
      const { id, payment } = answer.data;
      writeStored({ bookingId: id, serviceId: f.service.id, email: f.email });
      setBookingParam(id);
      setBookingId(id);
      setStart(payment);
      await pay(id, payment, false, f.email);
      return;
    }
    if (outcome.kind === "done" && answer.data) {
      setDone({
        status: answer.data.status,
        service: f.service,
        worker: f.service.workers.find((w) => w.id === (f.worker === "any" ? answer.data!.worker_id : f.worker)) ?? null,
        starts_at: answer.data.starts_at,
        price: answer.data.price,
        email: f.email,
        payment: null,
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
          : kind === "paymentsUnavailable"
            ? t("payUnavailable")
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
    if (outcome.kind === "paymentChanged") {
      // The options changed under the page: load them again, and say so by the button.
      router.refresh();
      setFlow({ ...f, busy: false, banner: { where: "c", tone: "note", text: t("paymentChanged") } });
      return;
    }
    if (outcome.kind === "phoneInvalid") {
      // ZIF-152: under the field, never the banner.
      setFlow({ ...f, busy: false, banner: null, errors: { ...f.errors, phone: t("errPhoneInvalid") } });
      focusOn("phone");
      return;
    }
    if (outcome.kind === "messageInvalid") {
      // B1: under the message, which opens if it was folded; never the generic banner.
      setFlow({ ...f, busy: false, banner: null, errors: { ...f.errors, message: t("errMessage") } });
      setMessageOpen(true);
      focusOn("message");
      return;
    }
    setFlow({ ...f, busy: false, banner: { where: "c", tone: "error", text: failureText(outcome.kind) } });
  }

  useEffect(() => {
    doBookRef.current = doBook;
  });

  // A booking in the address (a refresh, Back from a bank page, Stripe's return) is resumed; an id
  // stored for this service by an earlier visit is looked at once. Stripe's return also leaves its
  // secrets in the address: they go first.
  useEffect(() => {
    const clean = returnParams(window.location.search);
    if (clean.toString() !== new URLSearchParams(window.location.search).toString()) window.history.replaceState(null, "", `${window.location.pathname}${clean.toString() ? `?${clean}` : ""}`);
    const stored = readStored();
    // The validated prop, not the raw address: ?booking=junk is no booking.
    const search = new URLSearchParams(window.location.search);
    if (booking) search.set("booking", booking);
    else search.delete("booking");
    const mode = entryMode(search, stored);
    if (mode === "checkout") return;
    const id = mode === "resume" ? booking! : stored!.bookingId;
    let cancelled = false;
    void (async () => {
      const answer = await checkBooking(id);
      if (cancelled) return;
      const known = answer.status === 200 && answer.data;
      const gone = answer.status === 404 || answer.status === 422;
      if (mode === "resume") {
        setChecking(false);
        if (known) apply(answer.data!, stored?.bookingId === id ? stored.email : null);
        else if (gone) {
          forget(false);
          setBookingId(null);
          void ensureWeek(flow.service!, flow.worker, flow.weekStart);
        } else setEnd("checkEmail");
      } else if (known) {
        const next = storedNext(answer.data!);
        if (next === "offerResume") setOffer(answer.data!);
        else if (next === "done") apply(answer.data!, stored!.email);
        else writeStored(null);
      } else if (gone) writeStored(null);
    })();
    return () => {
      cancelled = true;
    };
    // Mount only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // A payment still being processed: ask again on focus and every 10 s for 2 minutes. After that the
  // page keeps its text, which already says an email follows.
  useEffect(() => {
    if (end !== "processing" || !bookingId) return;
    const began = Date.now();
    const again = async () => {
      const answer = await checkBooking(bookingId);
      if (answer.status === 200 && answer.data) apply(answer.data, storedEmail());
    };
    const timer = setInterval(() => (Date.now() - began > 120_000 ? clearInterval(timer) : void again()), 10_000);
    window.addEventListener("focus", again);
    return () => {
      clearInterval(timer);
      window.removeEventListener("focus", again);
    };
    // apply and checkBooking only read props and set state.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [end, bookingId]);

  const s2 = flow.service ? (flow as Step2Plus) : null;
  if (!s2) return null;
  const s2With = withName(s2.service.workers, s2.worker, s2.pick, t("anyone"));
  const detailsOpen = flow.step === 3;
  const auto = page.auto_confirm;
  const offerWhen = offer ? (({ date, time }) => `${date}, ${time}`)(localWhen(offer.starts_at, zone, dLocale)) : "";
  const price = formatMoney((sync?.price ?? s2.service.price).amount_minor, (sync?.price ?? s2.service.price).currency, locale);
  // A resumed booking without its secret yet shows the kind it was made with.
  const shown = start === null && sync ? sync.payment.kind : choice;
  const sendText = flow.busy ? (auto ? t("booking") : t("sending")) : t(sendLabel(stepOn ? shown : null, auto), { price });
  const extraTerms = termsLines(stepOn ? shown : null, Boolean(page.payment?.keeps_card_fee));

  // The page's frame: heading and business line, and (unless the booking is no longer open) the way back.
  const frame = (body: ReactNode, back = true) => (
    <>
      <main className={styles.screen}>
        <div className={`${styles.col} ${styles.bookingWide}`}>
          {back && (
            <Link className={styles.back} href={`/${page.slug}`}>
              <svg aria-hidden="true" viewBox="0 0 12 12" width="12" height="12">
                <path d="M7.5 2.5l-3 3.5 3 3.5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
              {t("backToServices")}
            </Link>
          )}
          <div className={styles.bizHeader}>
            <h1>{t("checkoutTitle")}</h1>
            <p className={styles.bizMeta}>
              <PinIcon />
              <span>{page.name}</span>
              <span aria-hidden="true">·</span>
              <span>{t("timesIn", { city: zoneCity(zone) })}</span>
            </p>
          </div>
          {body}
        </div>
      </main>
      <BookingFooter version={version} />
      {/* onReady, not onLoad: next/script's LoadCache means onLoad only fires the very first time this
       * script URL is ever loaded on the page — a client-side locale switch or soft nav remounts this
       * page without the browser reloading the script, so onLoad would never fire again and no
       * widget would ever get rendered. onReady fires on every mount, script-cached or not. */}
      {turnstileSiteKey && <Script src="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit" strategy="afterInteractive" onReady={onTurnstileLoad} />}
    </>
  );
  // A booking that waits for its payment, as the summary shows it: only what the booking itself says.
  const bookingAside = (b: SyncOut) => (
    <Summary
      page={page}
      service={{ ...(liveService ?? s2.service), name: b.service_name, duration_minutes: b.duration_minutes, price: b.price }}
      slot={b.starts_at}
      workerName={b.worker_display_name}
      locale={locale}
      dLocale={dLocale}
      zone={zone}
      t={t}
      message=""
      messageOpen={false}
      messageError={undefined}
      messageRef={messageRef}
      extraTerms={extraTerms}
      locked
      onMessage={null}
      onMessageOpen={setMessageOpen}
    />
  );
  const endText = (kind: End) => t(({ processing: "payProcessing", expired: "payExpired", expiredPaid: "payExpiredPaid", expiredProcessing: "payExpiredProcessing", stale: "payStale", checkEmail: "payCheckEmail" } as const)[kind], { price });
  if (checking) return frame(null, false);
  if (end === "processing" && sync) return frame(<Processing aside={bookingAside(sync)} t={t} />, false);
  // A check that failed keeps the booking: its button asks again, never resets (mayForget).
  if (end) {
    return frame(
      <PayEnd
        text={endText(end)}
        label={t(mayForget(end) ? "step2" : "tryAgain")}
        busy={flow.busy}
        failed={recheckFailed ? t("payCheckEmail") : null}
        onAgain={mayForget(end) ? reset : () => void recheck()}
      />,
      false,
    );
  }
  if (sync && !done) {
    return frame(
      <ResumeView
        page={page}
        price={sync.price}
        payment={stepOn && !payGone ? page.payment : null}
        choice={shown}
        busy={flow.busy}
        label={sendText}
        banner={flow.banner?.where === "c" ? flow.banner : null}
        aside={bookingAside(sync)}
        stepRef={stepRef}
        locale={locale}
        t={t}
        onChoice={chooseKind}
        onSend={() => void resend(sync.id, storedEmail())}
        onChange={() => void changeRequest()}
        onUnavailable={() => fail(t("payUnavailable"))}
      />,
      false,
    );
  }
  if (done) {
    return (
      <>
        <main className={styles.screen}>
          <div className={`${styles.col} ${styles.bookingWide}`}>
            <DoneScreen page={page} done={done} locale={loc} dLocale={dLocale} zone={zone} t={t} onAgain={() => router.push(`/${page.slug}`)} />
          </div>
        </main>
        <BookingFooter version={version} />
      </>
    );
  }

  return frame(
      <div className={styles.bookingLayout}>
        <div className={styles.flowCol}>
          {offer && !locked && (
            <div className={`${styles.stepBody} ${styles.payNote}`}>
              <Banner tone="note">
                <span className={styles.payNoteBody}>
                  {t("payInProgress", { service: localized(offer.service_name, loc, page.language as Locale).text, when: offerWhen })}
                  <span className={styles.emptyActs}>
                    <button className={`${styles.button} ${styles.primary} ${styles.small}`} type="button" onClick={resumeOffer}>
                      {t("payResume")}
                    </button>
                    <button className={`${styles.button} ${styles.secondary} ${styles.small}`} type="button" onClick={() => void startOver()}>
                      {t("payStartOver")}
                    </button>
                  </span>
                </span>
              </Banner>
            </div>
          )}
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
              <fieldset className={styles.lockSet} disabled={locked}>
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
              </fieldset>
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
                <WhoAt s2={s2} locked={locked} banner={flow.banner?.where === 3 ? flow.banner : null} zone={zone} dLocale={dLocale} t={t} onPick={(pick) => setFlow((f) => ({ ...f, pick }))} />
                <form
                  className={styles.form}
                  method="post"
                  noValidate
                  onSubmit={(event) => {
                    event.preventDefault();
                    void book();
                  }}
                >
                  <fieldset className={styles.lockSet} disabled={locked}>
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
                        <DialPicker options={dials} value={region} onChange={setRegion} />
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
                  </fieldset>
                  {payGone && locked && <Banner tone="note">{t("paymentChanged")}</Banner>}
                  {stepOn && !payGone && liveService && page.payment && (
                    <PaymentStep ref={stepRef} payment={page.payment} price={liveService.price} locale={locale} biz={page.name} choice={choice} busy={flow.busy} legend t={t} onChoice={chooseKind} onUnavailable={() => fail(t("payUnavailable"))} />
                  )}
                  {/* Rendered visibly (never display:none) and kept mounted once the details are
                   * open: an interaction-only challenge must be able to actually appear here. */}
                  {turnstileSiteKey && <div id="turnstile-container" className={styles.turnstileBox} />}
                  {verifying && <Banner tone="note">{t("verifying")}</Banner>}
                  {flow.banner?.where === "c" && <Banner tone={flow.banner.tone}>{flow.banner.text}</Banner>}
                  {!(payGone && locked) && (
                    <Submit busy={flow.busy} busyLabel={sendText}>
                      {sendText}
                    </Submit>
                  )}
                  {locked && (
                    <button className={`${styles.button} ${styles.secondary}`} type="button" onClick={() => void changeRequest()}>
                      {t("changeRequest")}
                    </button>
                  )}
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
          extraTerms={extraTerms}
          locked={locked}
          onMessage={(text) => {
            setMessage(text);
            if (flow.errors.message) setFlow((f) => ({ ...f, errors: { ...f.errors, message: undefined } }));
          }}
          onMessageOpen={setMessageOpen}
        />
      </div>
  );
}
