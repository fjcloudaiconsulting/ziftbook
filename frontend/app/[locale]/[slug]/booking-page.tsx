"use client";

import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { availabilityRead, bookingHoldsCreate, bookingHoldsRelease, bookingsCreate, type BookingPageOut, sessionRead } from "@/api-client";
import { Link } from "@/i18n/navigation";
import { addDays, localDay, localWhen, slotsByDay, weekDays } from "@/lib/booking-link";
import {
  afterPickTaken,
  type AnswerOutcome,
  answerState,
  bookingBody,
  emailSuggestion,
  firstFreeDayFrom,
  holdBody,
  keepPick,
  nextWeekDisabled,
  scanWindow,
  signedInAs,
  slotsFor,
  slotWho,
  withName,
} from "@/lib/booking-page";
import { dateLocale } from "@/lib/console";
import { formatMoney } from "@/lib/money";
import { type Locale } from "@/lib/services";
import { zoneCity } from "@/lib/week";

import { Banner, FieldError, Outcome, send } from "../_ui/parts";
import { CheckIcon } from "../_ui/picker";
import { ServiceTile } from "../_ui/service-tile";
import styles from "../_ui/ui.module.css";
import { CheckEmail, type Sent } from "./email-first";
import { type Done, DoneScreen, Checkout, Picker, WhoAt } from "./checkout";
import { PinIcon } from "./icons";
import { ServiceList } from "./service-list";
import { type CacheEntry, type Flow, localized, type Service, type Step2Plus, type TimesWindow } from "./shared";

const TURNSTILE_LANG: Record<string, string> = { pt: "pt-br" };

const INITIAL: Omit<Flow, "name" | "email" | "phone"> = {
  step: 1,
  service: null,
  worker: "any",
  weekStart: "",
  day: null,
  slot: null,
  pick: null,
  free: [],
  taken: new Set(),
  errors: {},
  suggest: null,
  banner: null,
  busy: false,
  replaces: null,
};


export function BookingPage({ page, locale, turnstileSiteKey, initialService = null }: { page: BookingPageOut; locale: string; turnstileSiteKey: string | null; initialService?: string | null }) {
  const t = useTranslations("BookingPage");
  const router = useRouter();
  const loc = locale as Locale;
  const zone = page.timezone;
  const businessLanguage = page.language as Locale;
  const dLocale = dateLocale(locale);

  // ZIF-117: the confirm page's "Choose another time" lands here with ?service=: its times open.
  const [flow, setFlow] = useState<Flow>(() => {
    const service = page.services.find((svc) => svc.id === initialService) ?? null;
    if (!service) return { ...INITIAL, name: "", email: "", phone: "" };
    const worker = service.workers.length === 1 ? service.workers[0].id : "any";
    return { ...INITIAL, name: "", email: "", phone: "", step: 2, service, worker, weekStart: localDay(new Date(), page.timezone) };
  });
  const [done, setDone] = useState<Done | null>(null);
  const [sent, setSent] = useState<Sent | null>(null);
  // ZIF-117: the signed-in account's own address. Only that address books straight away; any other
  // gets a link by email first (signedInAs).
  const [sessionEmail, setSessionEmail] = useState<string | null>(null);
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
  const headingRefs = useRef<Record<number, HTMLHeadingElement | null>>({});
  const [focusStep, setFocusStep] = useState<number | null>(null);
  // Paired with a generation counter, not just the field name: pressing Book twice with the SAME
  // field still missing both times must still re-focus it — a plain "name"/"name" value wouldn't
  // change, so the effect below wouldn't re-fire.
  const [focusField, setFocusField] = useState<{ field: "name" | "email" | "phone"; gen: number } | null>(null);
  const focusFieldGen = useRef(0);
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

  // Autofill: GET /api/session once (the person's own name, not their name in some business); fill
  // name/email ONLY if still empty. Any other answer (no session, network, whatever) is ignored
  // silently.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const answer = await send(sessionRead());
      if (cancelled || answer.status !== 200 || !answer.data) return;
      setSessionEmail(answer.data.email);
      setFlow((f) => {
        const name = f.name || answer.data!.name || f.name;
        const email = f.email || answer.data!.email || f.email;
        sessionFill.current = { name: f.name ? sessionFill.current.name : name, email: f.email ? sessionFill.current.email : email };
        return { ...f, name, email };
      });
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // ZIF-117: with ?service=, that service's times load once mounted, a tick later (never a state
  // update in the effect's own body).
  useEffect(() => {
    if (!flow.service) return;
    const service = flow.service;
    const timer = setTimeout(() => void ensureWeek(service, flow.worker, flow.weekStart));
    return () => clearTimeout(timer);
    // Mount only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (focusStep !== null) headingRefs.current[focusStep]?.focus();
  }, [focusStep]);

  // Runs after step 3 has (re)rendered with its inputs mounted, so a field targeted while step 3
  // wasn't the open step still gets focused once it is.
  useEffect(() => {
    if (focusField?.field === "name") nameRef.current?.focus();
    else if (focusField?.field === "email") emailRef.current?.focus();
    else if (focusField?.field === "phone") phoneRef.current?.focus();
  }, [focusField, flow.step]);

  // One window for everyone (ZIF-100): a person's times are filtered out of it locally (slotsFor).
  function cacheKey(service: string, from: string) {
    return `${service}|${from}`;
  }

  /** Sends the client back to step 1 with a note, and re-fetches the page's own data (a service
   * that just 404'd may be gone from `page` itself too). Used for both a 404 on POST /bookings
   * and a 404 on the week GET — the spec gives both the same path. */
  function backToStep1(noteText: string) {
    router.refresh();
    setFlow((f) => ({ ...INITIAL, name: f.name, email: f.email, phone: f.phone, replaces: f.replaces, banner: { where: 1, tone: "note", text: noteText } }));
    setFocusStep(1);
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
      // The service (or every worker on it) is gone: same path as a 404 on Book (spec's mapping).
      backToStep1(t("serviceGone"));
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

  async function ensureWeek(service: Service, worker: string, weekStart: string, bypass = false) {
    const entryKey = `${service.id}|${worker}`;
    const myToken = (ensureWeekToken.current.get(entryKey) ?? 0) + 1;
    ensureWeekToken.current.set(entryKey, myToken);
    const isCurrent = () => ensureWeekToken.current.get(entryKey) === myToken;

    const windowFrom = windowFromFor(weekStart, weekStart);
    const entry = await loadWindow(service, windowFrom, bypass);
    if (typeof entry === "string" || !isCurrent()) return;
    const mine = slotsFor(entry, worker);

    const visibleWeekEmpty = weekDays(weekStart).every((d) => !(slotsByDay(mine, zone).get(d) ?? []).length);
    if (!visibleWeekEmpty) return;

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
        if (typeof next === "string" || !isCurrent()) return;
        first = firstFreeDayFrom(slotsFor(next, worker), from, zone);
        if (first !== null) break;
      }
      // Reached here only by completing the scan (found something, hit the horizon, or the cap) —
      // never by bailing out early — so it's now safe to skip a repeat for this service/worker.
      scanned.current.add(entryKey);
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
    setFlow((f) => ({ ...f, step: 2, service, worker, weekStart, day: null, slot: null, pick: null, taken: new Set(), banner: null }));
    setFocusStep(2);
    void ensureWeek(service, worker, weekStart);
  }

  /** Keeps the day (PickerWeek falls back to the first day with times when this person has none
   * that day); clears the time, which this person may not be free at. */
  function chooseWorker(worker: string) {
    setFlow((f) => (f.step >= 2 ? { ...f, worker, slot: null, pick: null, banner: f.banner?.where === 3 ? null : f.banner } : f));
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

  function pickSlot(slot: string, free: string[]) {
    setFlow((f) => (f.step >= 2 ? { ...f, slot, free, pick: keepPick(f.pick, f.service!.workers, free), step: 3, banner: f.banner?.where === 3 ? null : f.banner } : f));
    setFocusStep(3);
  }

  function goTo(step: 1 | 2 | 3) {
    setFlow((f) => ({ ...f, step }));
    setFocusStep(step);
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

  // Belt-and-suspenders for StrictMode's mount/unmount/remount: onReady is the path for the very
  // first script load, but if `window.turnstile` is already there (the script executed during an
  // earlier mount in the same cycle) and this mount has no widget of its own yet, render one
  // directly instead of waiting on a callback that may not fire again.
  useEffect(() => {
    const w = (window as unknown as { turnstile?: unknown }).turnstile;
    if (turnstileSiteKey && w && !turnstileWidget.current) onTurnstileLoad();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
    if (!linkFirst && !flow.name.trim()) errors.name = t("errName");
    if (!linkFirst && !flow.phone.trim()) errors.phone = t("errPhone");
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(flow.email.trim())) errors.email = t("errEmail");
    if (Object.keys(errors).length > 0) {
      // Step 3's inputs only exist in the DOM while it's the open step: open it first (if it
      // wasn't already) so the ref below actually points at a mounted field, then focus once it
      // has — a step 2 "Change" reopen must not leave the error unfocusable.
      setFlow((f) => ({ ...f, errors, step: 3 }));
      focusFieldGen.current += 1;
      setFocusField({ field: errors.name ? "name" : errors.phone ? "phone" : "email", gen: focusFieldGen.current });
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
    const memberId = f.pick ?? (f.worker === "any" ? null : f.worker);
    // ZIF-117: anyone but the signed-in address itself gets a link by email first ("Send the email
    // again" comes back here too, replacing the hold on screen).
    if (sent || linkFirst) return doHold(f, memberId, token);
    if (f.replaces) {
      // Back to the signed-in address after a link went elsewhere: that hold would block its own time.
      const released = await send(bookingHoldsRelease({ path: { tenant_id: page.id }, body: { secret: f.replaces } }));
      if (released.status === 204) setFlow((cur) => ({ ...cur, replaces: null }));
    }
    const body = bookingBody({
      startsAt: f.slot!,
      memberId,
      name: f.name,
      email: f.email,
      phone: f.phone,
      locale,
      policyVersion: page.policy_version,
      turnstileToken: token,
    });
    const answer = await send(bookingsCreate({ path: { tenant_id: page.id, service_id: f.service.id }, body: { ...body, locale: loc } }));
    working.current = false;
    const outcome = answerState(answer, Boolean(turnstileSiteKey));
    // Reset after EVERY answer, 201 included: a fresh "Book another" must never reuse this token.
    if (turnstileSiteKey && outcome.kind !== "verifyEmail") resetTurnstile();

    if (outcome.kind === "verifyEmail") {
      // The session ended, or isn't this address's: refused before Turnstile, so the same token
      // goes to the hold instead, and the form becomes the email-only one.
      setSessionEmail(null);
      return doHold(f, memberId, token);
    }
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
    await afterRefusal(f, outcome);
  }

  /** ZIF-117: the hold POST, a first link or "Send the email again" (which replaces the hold on
   * screen; "Wrong address" leaves its secret in flow.replaces). */
  async function doHold(f: Step2Plus, memberId: string | null, token: string | null) {
    const again = sent;
    const replaces = again?.secret ?? f.replaces;
    working.current = true;
    const held = await send(
      bookingHoldsCreate({
        path: { tenant_id: page.id, service_id: f.service.id },
        body: { ...holdBody({ startsAt: f.slot!, memberId, email: f.email, locale: loc, turnstileToken: token, replaces }), locale: loc },
      }),
    );
    working.current = false;
    if (turnstileSiteKey) resetTurnstile();
    const outcome = answerState(held, Boolean(turnstileSiteKey));
    if (outcome.kind === "done" && held.data) {
      const email = f.email.trim();
      setSent({
        secret: held.data.secret,
        email,
        startsAt: held.data.starts_at,
        heldUntil: held.data.expires_at,
        service: f.service,
        who: withName(f.service.workers, f.worker, f.pick, t("anyone")),
        notice: again ? { tone: "note", text: t("sentAgain", { email }) } : null,
      });
      setFlow((cur) => ({ ...cur, busy: false, replaces: null }));
      return;
    }
    if (again && outcome.kind !== "slotTaken" && outcome.kind !== "serviceGone") {
      // Stays on "Check your email": the hold and its link are as they were.
      setSent({ ...again, notice: { tone: "error", text: failureText(outcome.kind) } });
      setFlow((cur) => ({ ...cur, busy: false }));
      return;
    }
    // Refused: whatever the booker does next, its hold still replaces the one it had.
    setSent(null);
    setFlow((cur) => ({ ...cur, replaces }));
    return afterRefusal({ ...f, replaces }, outcome);
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

  /** Every refused POST, today's or the hold's: the time taken, the service gone, the terms
   * changed, or a banner by the button. */
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
        ...(cur.slot === f.slot ? { slot: null, pick: null, step: 2 as const, banner: { where: 2 as const, tone: "note" as const, text: t("slotTaken", { time }) } } : {}),
      }));
      setFocusStep(2);
      // The cached window is stale (it still offers the just-taken slot): bypass it, unless the
      // pick's refetch above just refreshed it.
      void ensureWeek(f.service, f.worker, f.weekStart, fresh === "error" || slotWindow !== f.weekStart);
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
    if (outcome.kind === "phoneInvalid") {
      // ZIF-152: under the field, never the banner; step 3 reopens if "Change" left it.
      setFlow({ ...f, busy: false, banner: null, step: 3, errors: { ...f.errors, phone: t("errPhoneInvalid") } });
      focusFieldGen.current += 1;
      setFocusField({ field: "phone", gen: focusFieldGen.current });
      return;
    }
    setFlow({ ...f, busy: false, banner: { where: "c", tone: "error", text: failureText(outcome.kind) } });
  }

  /** "Choose another time": the hold goes (its link with it) and the times show it free again. */
  async function otherTime() {
    if (!sent || working.current) return;
    working.current = true;
    await send(bookingHoldsRelease({ path: { tenant_id: page.id }, body: { secret: sent.secret } }));
    working.current = false;
    setSent(null);
    const f = flow as Step2Plus;
    // Kept for the next hold anyway: if the release didn't land, that hold replaces this one.
    setFlow((cur) => ({ ...cur, step: 2, slot: null, pick: null, banner: null, replaces: sent.secret }));
    setFocusStep(2);
    void ensureWeek(f.service, f.worker, f.weekStart, true);
  }

  /** "Wrong address? Change it": back to the address; the next link replaces this hold. */
  function wrongAddress() {
    if (!sent) return;
    setFlow((cur) => ({ ...cur, step: 3, replaces: sent.secret }));
    setSent(null);
    focusFieldGen.current += 1;
    setFocusField({ field: "email", gen: focusFieldGen.current });
  }
  useEffect(() => {
    doBookRef.current = doBook;
  });

  const linkFirst = !signedInAs(sessionEmail, flow.email);
  const service1Done = flow.service;
  // flow.service is checked, not just flow.step: TS doesn't propagate that narrowing through the
  // ternary's inferred type, so the cast documents what the runtime check already guarantees.
  // Not gated by flow.step: the checkout summary (and step 2/3's own folded rows) must keep
  // showing what's already chosen even while step 1 is reopened via "Change".
  const s2 = flow.service ? (flow as Step2Plus) : null;
  const s2With = s2 ? withName(s2.service.workers, s2.worker, s2.pick, t("anyone")) : null;

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
            ) : sent ? (
              <CheckEmail
                page={page}
                sent={sent}
                locale={loc}
                dLocale={dLocale}
                zone={zone}
                t={t}
                busy={flow.busy}
                verifying={verifying}
                banner={flow.banner?.where === "c" ? flow.banner : null}
                onAgain={() => void book()}
                onOtherTime={() => void otherTime()}
                onWrongAddress={wrongAddress}
              />
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
                    <ServiceList page={page} selected={flow.service?.id} loc={loc} businessLanguage={businessLanguage} locale={locale} t={t} onSelect={selectService} />
                  ) : (
                    service1Done && (
                      <div className={styles.stepDone}>
                        <ServiceTile placeholder={service1Done.placeholder} photo={service1Done.photo?.thumb_url} className={styles.tile40} />
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
                        {s2With !== null && <span>{t("with", { name: s2With })}</span>}
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
                    {linkFirst ? t("step3Email") : t("step3")}
                  </h2>
                </div>
                <div className={styles.stepBody}>
                {s2 && flow.step === 3 && <WhoAt s2={s2} banner={flow.banner?.where === 3 ? flow.banner : null} zone={zone} dLocale={dLocale} t={t} onPick={(pick) => setFlow((f) => ({ ...f, pick }))} />}
                {s2 && flow.step === 3 && (
                  <div className={styles.form}>
                    {/* ZIF-117: name and phone only for the signed-in address itself; anyone else
                     * gives them on the confirm page, after the emailed link. */}
                    {!linkFirst && (
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
                          {t("phone")}
                        </label>
                        <div className={styles.input}>
                          <input
                            ref={phoneRef}
                            id="bp-phone"
                            name="phone"
                            type="tel"
                            autoComplete="tel"
                            maxLength={40}
                            value={flow.phone}
                            onChange={(e) => fieldChange("phone", e.target.value)}
                            aria-invalid={flow.errors.phone ? true : undefined}
                            aria-describedby={flow.errors.phone ? "bp-phone-hint bp-phone-err" : "bp-phone-hint"}
                          />
                        </div>
                        <p className={styles.hint} id="bp-phone-hint">
                          {t("phoneHint", { biz: page.name, country: t(`phoneCountry.${page.country}`) })}
                        </p>
                        {flow.errors.phone && <FieldError id="bp-phone-err">{flow.errors.phone}</FieldError>}
                      </div>
                    </div>
                    )}
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
                        {linkFirst ? t("emailLinkHint") : t("emailHint")}
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
            bare={sent !== null}
            linkFirst={linkFirst}
          />
        </div>
      </div>
    </main>
    <footer className={styles.minimalFooter}>
      <span>
        {t("footerBy")} <b>ziftbook</b>
      </span>
      {/* ZIF-147. A new tab: leaving would lose a booking half filled in. */}
      <Link href="/terms" target="_blank">
        {t("footerTerms")}
        <span className={styles.srOnly}> {t("newTab")}</span>
      </Link>
      <Link href="/privacy" target="_blank">
        {t("footerPrivacy")}
        <span className={styles.srOnly}> {t("newTab")}</span>
      </Link>
    </footer>
    </>
  );
}
