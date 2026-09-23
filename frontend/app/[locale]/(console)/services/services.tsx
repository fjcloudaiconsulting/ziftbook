"use client";

import { useLocale, useTranslations } from "next-intl";
import { type FormEvent, type KeyboardEvent, useEffect, useId, useMemo, useRef, useState } from "react";

import {
  type MemberOut,
  type ServiceOut,
  membersList,
  servicesCreate,
  servicesList,
  servicesRead,
  servicesReplaceWorkers,
  servicesUpdate,
} from "@/api-client";
import { Link, useRouter } from "@/i18n/navigation";
import { currencySign, formatMoney, parseMoney, priceText, signFirst } from "@/lib/money";
import {
  activeServices,
  archivedServices,
  defaultBuffer,
  type Locale,
  type NameMap,
  needsWorkerWarning,
  serviceBody,
  type ServiceForm,
  serviceName,
} from "@/lib/services";

import { SignedOutBanner, useConsole } from "../../_ui/console";
import { Banner, FieldError, Heading, Mark, problem, Submit } from "../../_ui/parts";
import styles from "../../_ui/ui.module.css";

const LOCALES: Locale[] = ["en", "nl", "pt"];

function useCurrencyName(currency: string, locale: string): string {
  return useMemo(() => {
    try {
      return new Intl.DisplayNames(locale, { type: "currency" }).of(currency) ?? currency;
    } catch {
      return currency;
    }
  }, [currency, locale]);
}

/* ---------------------------------- List ---------------------------------- */

/** Owner row: a whole-row link to the edit form, with the people count or the "no one assigned"
 * warning pill and a chevron, as drawn (`s-services-list`). */
function Row({ service, locale }: { service: ServiceOut; locale: string }) {
  const t = useTranslations("Console.services");
  const { settings } = useConsole();
  const name = serviceName(service.name as NameMap, locale as Locale, settings.language);
  const price = formatMoney(service.price.amount_minor, service.price.currency, locale);
  const n = service.worker_ids.length;
  const warn = needsWorkerWarning(n);

  return (
    <li>
      <Link href={`/services/${service.id}`} className={styles.rowLink}>
        <span className={styles.rowMain}>
          <span className={styles.rowTitle}>{name}</span>
          {!warn && <span className={styles.rowMeta}>{t("rowMetaPeople", { minutes: service.duration_minutes, price, n })}</span>}
          {warn && (
            <>
              <span className={styles.rowMeta}>{t("rowMeta", { minutes: service.duration_minutes, price })}</span>
              <span className={`${styles.pill} ${styles.pillWarn}`}>
                <svg aria-hidden="true" viewBox="0 0 16 16" width="12" height="12">
                  <circle cx="8" cy="8" r="6.5" fill="none" stroke="currentColor" strokeWidth="1.6" />
                  <path d="M8 4.5v4M8 11h.01" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
                </svg>
                {t("warningPill")}
              </span>
            </>
          )}
        </span>
        <svg className={styles.chev} aria-hidden="true" viewBox="0 0 12 12" width="12" height="12">
          <path d="M4.5 2.5l3 3.5-3 3.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </Link>
    </li>
  );
}

/** Worker row: static, no link and no chevron — a worker has no edit route to go to
 * (`allowed()` in lib/console.ts already blocks it), and no people count (`/api/members` is
 * owner only, `s-services-worker`). */
function WorkerRow({ service, locale }: { service: ServiceOut; locale: string }) {
  const t = useTranslations("Console.services");
  const { settings } = useConsole();
  const name = serviceName(service.name as NameMap, locale as Locale, settings.language);
  const price = formatMoney(service.price.amount_minor, service.price.currency, locale);

  return (
    <li>
      <div className={styles.rowStatic}>
        <span className={styles.rowMain}>
          <span className={styles.rowTitle}>{name}</span>
          <span className={styles.rowMeta}>{t("rowMeta", { minutes: service.duration_minutes, price })}</span>
        </span>
      </div>
    </li>
  );
}

function ArchivedRow({ service, locale, onBroughtBack }: { service: ServiceOut; locale: string; onBroughtBack(service: ServiceOut): void }) {
  const t = useTranslations("Console.services");
  const form = useTranslations("Form");
  const { settings, call } = useConsole();
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<ReturnType<typeof problem> | null>(null);
  const [signedOut, setSignedOut] = useState(false);
  // A ref, not `busy` state: several calls in the same tick (e.g. a double click before React
  // re-renders) all read the same stale `busy` from their render closure and would all pass a
  // state-based guard. The ref is set synchronously, before any `await`, so only the first call
  // in a tick ever gets past it.
  const inFlight = useRef(false);
  const name = serviceName(service.name as NameMap, locale as Locale, settings.language);
  const price = formatMoney(service.price.amount_minor, service.price.currency, locale);

  async function bringBack() {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setFailure(null);
    setSignedOut(false);
    try {
      const outcome = await call(() => servicesUpdate({ path: { service_id: service.id }, body: { archived: false } }), { write: true });
      if (outcome.status === 200 && outcome.data) onBroughtBack(outcome.data);
      else if (outcome.status === 401) setSignedOut(true);
      else setFailure(problem(outcome));
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  return (
    <li>
      <div className={styles.rowStatic}>
        <span className={styles.rowMain}>
          <span className={styles.rowTitle}>{name}</span>
          <span className={styles.rowMeta}>{t("archivedMeta", { minutes: service.duration_minutes, price })}</span>
        </span>
        <span className={styles.rowEnd}>
          <button className={`${styles.button} ${styles.secondary} ${styles.small}`} type="button" aria-disabled={busy || undefined} onClick={bringBack}>
            {t("bringBack")}
          </button>
        </span>
      </div>
      {/* A <div> (SignedOutBanner) or role="alert" element can't nest inside .rowMain's <span>
       * (phrasing content only), and a plain muted <span> is never announced to screen readers:
       * both render as block-level siblings of the row instead. */}
      {signedOut && <SignedOutBanner />}
      {!signedOut && failure && (
        <p role="alert" className={styles.rowMeta}>
          {form(failure)}
        </p>
      )}
    </li>
  );
}

export function Services() {
  const { call, session, settings } = useConsole();
  const t = useTranslations("Console.services");
  const nav = useTranslations("Console.nav");
  const form = useTranslations("Form");
  const locale = useLocale();

  const [services, setServices] = useState<ServiceOut[] | null>(null);
  const [failure, setFailure] = useState<ReturnType<typeof problem> | null>(null);

  function load() {
    call(() => servicesList()).then((outcome) => {
      if (outcome.status === 200 && outcome.data) {
        setFailure(null);
        setServices(outcome.data);
      } else {
        setFailure(problem(outcome));
      }
    });
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const collator = useMemo(() => new Intl.Collator(locale), [locale]);
  const active = useMemo(
    () =>
      activeServices(services ?? []).sort((a, b) =>
        collator.compare(
          serviceName(a.name as NameMap, locale as Locale, settings.language),
          serviceName(b.name as NameMap, locale as Locale, settings.language),
        ),
      ),
    [services, collator, locale, settings.language],
  );
  const archived = useMemo(() => archivedServices(services ?? []), [services]);

  function onBroughtBack(updated: ServiceOut) {
    setServices((current) => (current ? current.map((s) => (s.id === updated.id ? updated : s)) : current));
  }

  if (failure) {
    return (
      <>
        <Heading focus>{nav("services")}</Heading>
        <Banner tone="error">{form(failure)}</Banner>
        <button className={styles.textButton} type="button" onClick={load}>
          {form("tryAgain")}
        </button>
      </>
    );
  }

  if (!services) return <Heading focus>{nav("services")}</Heading>;

  if (session.role === "worker") {
    return (
      <>
        <Heading focus>{nav("services")}</Heading>
        <Banner tone="note">{t("workerNote")}</Banner>
        <ul className={styles.list}>
          {active.map((service) => (
            <WorkerRow key={service.id} service={service} locale={locale} />
          ))}
        </ul>
      </>
    );
  }

  if (active.length === 0 && archived.length === 0) {
    return (
      <>
        <Heading focus>{nav("services")}</Heading>
        <div className={styles.empty}>
          <Mark icon="calendar" />
          <strong>{t("empty.title")}</strong>
          <span>{t("empty.body")}</span>
          <Link className={`${styles.button} ${styles.primary}`} href="/services/new">
            {t("addService")}
          </Link>
        </div>
      </>
    );
  }

  return (
    <>
      <div className={styles.screenHead}>
        <Heading focus>{nav("services")}</Heading>
        <Link className={`${styles.button} ${styles.primary} ${styles.small}`} href="/services/new">
          {t("addService")}
        </Link>
      </div>
      <ul className={styles.list}>
        {active.map((service) => (
          <Row key={service.id} service={service} locale={locale} />
        ))}
      </ul>
      {archived.length > 0 && (
        <details>
          <summary className={styles.textButton}>{t("archived", { n: archived.length })}</summary>
          <ul className={styles.list} style={{ marginTop: "0.6rem" }}>
            {archived.map((service) => (
              <ArchivedRow key={service.id} service={service} locale={locale} onBroughtBack={onBroughtBack} />
            ))}
          </ul>
          <p className={styles.hint} style={{ marginTop: "0.5rem" }}>
            {t("archivedHint")}
          </p>
        </details>
      )}
    </>
  );
}

/* ---------------------------------- Form ---------------------------------- */

type Errors = Partial<Record<"name" | "price" | "duration" | "gap", string>> & { banner?: string };

function emptyNameMap(): Record<Locale, string> {
  return { en: "", nl: "", pt: "" };
}

function LangField({
  heading,
  headingHint,
  values,
  onChange,
  businessLanguage,
  optionalHint,
  requiredHint,
  placeholder,
  multiline,
  errorMessage,
}: {
  heading: string;
  headingHint?: string;
  values: Record<Locale, string>;
  onChange(locale: Locale, value: string): void;
  businessLanguage: Locale;
  optionalHint?: string;
  requiredHint?: string;
  placeholder?(language: string): string;
  multiline?: boolean;
  errorMessage?: string;
}) {
  const t = useTranslations("Console.services.form");
  const [active, setActive] = useState<Locale>(businessLanguage);
  const idBase = useId();
  const errorId = `${idBase}-error`;
  const headingId = `${idBase}-heading`;
  const tabId = (loc: Locale) => `${idBase}-tab-${loc}`;
  const tabRefs = useRef<Partial<Record<Locale, HTMLButtonElement | null>>>({});

  // Roving tabindex + arrow-key navigation, per the ARIA APG tablist pattern: only the selected
  // tab sits in the page's Tab order, and Left/Right/Home/End move both the selection and focus
  // between the other two.
  function onTabKeyDown(event: KeyboardEvent, loc: Locale) {
    const index = LOCALES.indexOf(loc);
    let nextIndex: number;
    if (event.key === "ArrowRight") nextIndex = (index + 1) % LOCALES.length;
    else if (event.key === "ArrowLeft") nextIndex = (index - 1 + LOCALES.length) % LOCALES.length;
    else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = LOCALES.length - 1;
    else return;
    event.preventDefault();
    const next = LOCALES[nextIndex];
    setActive(next);
    tabRefs.current[next]?.focus();
  }

  return (
    <div className={styles.field}>
      <div className={styles.labelRow}>
        <span className={styles.label} id={headingId}>
          {heading} {headingHint && <span className={styles.optional}>{headingHint}</span>}
        </span>
      </div>
      <div className={styles.langTabs} role="tablist" aria-labelledby={headingId}>
        {LOCALES.map((loc) => (
          <button
            key={loc}
            ref={(el) => {
              tabRefs.current[loc] = el;
            }}
            id={tabId(loc)}
            type="button"
            role="tab"
            aria-selected={active === loc}
            aria-controls={`${idBase}-${loc}`}
            tabIndex={active === loc ? 0 : -1}
            data-filled={values[loc].trim() ? "yes" : "no"}
            className={styles.langTab}
            onClick={() => setActive(loc)}
            onKeyDown={(event) => onTabKeyDown(event, loc)}
          >
            <span className={styles.dot} aria-hidden="true" />
            {t(`languages.${loc}`)}
          </button>
        ))}
      </div>
      {LOCALES.map((loc) => (
        <div
          key={loc}
          id={`${idBase}-${loc}`}
          role="tabpanel"
          aria-labelledby={tabId(loc)}
          hidden={active !== loc}
          className={styles.langPanel}
        >
          {multiline ? (
            <>
              <label className={styles.srOnly} htmlFor={`${idBase}-${loc}-input`}>
                {t("descriptionLangLabel", { language: t(`languages.${loc}`) })}
              </label>
              <div className={styles.input}>
                <textarea
                  id={`${idBase}-${loc}-input`}
                  maxLength={1000}
                  placeholder={loc === businessLanguage ? placeholder?.(t(`languages.${businessLanguage}`)) : undefined}
                  value={values[loc]}
                  onChange={(event) => onChange(loc, event.target.value)}
                />
              </div>
            </>
          ) : (
            <>
              <label className={styles.label} htmlFor={`${idBase}-${loc}-input`}>
                {t("nameLangLabel", { language: t(`languages.${loc}`) })}
                {(loc === businessLanguage ? requiredHint : optionalHint) && (
                  <>
                    {" "}
                    <span className={styles.optional}>{loc === businessLanguage ? requiredHint : optionalHint}</span>
                  </>
                )}
              </label>
              <div className={styles.input}>
                <input
                  id={`${idBase}-${loc}-input`}
                  type="text"
                  maxLength={100}
                  autoComplete="off"
                  placeholder={loc !== businessLanguage ? placeholder?.(t(`languages.${businessLanguage}`)) : undefined}
                  value={values[loc]}
                  onChange={(event) => onChange(loc, event.target.value)}
                  aria-invalid={loc === businessLanguage && errorMessage ? true : undefined}
                  aria-describedby={loc === businessLanguage && errorMessage ? errorId : undefined}
                />
              </div>
            </>
          )}
        </div>
      ))}
      {!multiline && errorMessage && <FieldError id={errorId}>{errorMessage}</FieldError>}
    </div>
  );
}

type Props = { mode: "create" } | { mode: "edit"; service: ServiceOut; members: MemberOut[] };

function ServiceFormBody(props: Props) {
  const t = useTranslations("Console.services.form");
  const services = useTranslations("Console.services");
  const consoleForm = useTranslations("Form");
  const locale = useLocale();
  const router = useRouter();
  const { call, session, settings } = useConsole();
  const currencyName = useCurrencyName(session.currency, locale);

  const editing = props.mode === "edit";
  const service = editing ? props.service : null;

  const [name, setName] = useState<Record<Locale, string>>(() =>
    service ? { ...emptyNameMap(), ...(service.name as NameMap) } : emptyNameMap(),
  );
  const [description, setDescription] = useState<Record<Locale, string>>(() =>
    service ? { ...emptyNameMap(), ...(service.description as NameMap) } : emptyNameMap(),
  );
  const [price, setPrice] = useState(() => (service ? priceText(service.price.amount_minor, locale) : ""));
  const [duration, setDuration] = useState(() => (service ? String(service.duration_minutes) : "45"));
  const [gap, setGap] = useState<"default" | "fixed">(() => (service?.buffer_minutes != null ? "fixed" : "default"));
  const [fixedGap, setFixedGap] = useState(() => (service?.buffer_minutes != null ? String(service.buffer_minutes) : "0"));

  const [members, setMembers] = useState<MemberOut[] | null>(editing ? props.members : null);
  const [checked, setChecked] = useState<Set<string>>(() => new Set(editing ? props.service.worker_ids : [session.member_id]));

  const [errors, setErrors] = useState<Errors>({});
  const [banner, setBanner] = useState<ReturnType<typeof problem> | "unknownMember" | null>(null);
  const [signedOut, setSignedOut] = useState(false);
  const [membersFailure, setMembersFailure] = useState<ReturnType<typeof problem> | null>(null);
  const [busy, setBusy] = useState(false);
  const [failedOnce, setFailedOnce] = useState(false);
  const submitting = useRef(false);

  useEffect(() => {
    if (props.mode !== "create") return;
    call(() => membersList()).then((outcome) => {
      if (outcome.status === 200 && outcome.data) setMembers(outcome.data);
      else setMembersFailure(problem(outcome));
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function markDirty() {
    setFailedOnce(false);
  }

  const sign = currencySign(0, session.currency, locale);
  const trailing = !signFirst(session.currency, locale);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    // A ref, not `busy` state: several submits in the same tick (Enter plus a click, or a
    // double-tap, before React re-renders) all read the same stale `busy` from their render
    // closure and would all pass a state-based guard. The ref is set synchronously, before any
    // `await`, so only the first call in a tick ever gets past it.
    if (submitting.current) return;
    const form: ServiceForm = { name, description, price, duration, gap, fixedGap };
    const result = serviceBody(form, { businessLanguage: settings.language, mode: editing ? "edit" : "create" }, parseMoney);
    if ("errors" in result && result.errors) {
      setErrors(result.errors);
      return;
    }
    submitting.current = true;
    setErrors({});
    setBanner(null);
    setSignedOut(false);
    setBusy(true);

    try {
      if (!editing) {
        const workerIds = [...checked];
        const outcome = await call(() => servicesCreate({ body: { ...result.body, worker_ids: workerIds } }), { write: true });
        if (outcome.status === 201) {
          router.push("/services");
          return;
        }
        if (outcome.status === 401) {
          setSignedOut(true);
          return;
        }
        if (outcome.status === 422 && outcome.code === "unknown_member") setBanner("unknownMember");
        else setBanner(problem(outcome));
        setFailedOnce(true);
        return;
      }

      // edit: servicesUpdate, then servicesReplaceWorkers only if the set changed. Both idempotent.
      const before = new Set(props.service.worker_ids);
      const after = checked;
      const workersChanged = before.size !== after.size || [...before].some((id) => !after.has(id));

      const updateOutcome = await call(() => servicesUpdate({ path: { service_id: props.service.id }, body: result.body }), { write: true });
      if (updateOutcome.status !== 200 || !updateOutcome.data) {
        if (updateOutcome.status === 401) {
          setSignedOut(true);
          return;
        }
        setBanner(problem(updateOutcome));
        setFailedOnce(true);
        return;
      }
      if (!workersChanged) {
        router.push("/services");
        return;
      }
      const workersOutcome = await call(() => servicesReplaceWorkers({ path: { service_id: props.service.id }, body: [...after] }), { write: true });
      if (workersOutcome.status === 200) {
        router.push("/services");
        return;
      }
      if (workersOutcome.status === 401) {
        setSignedOut(true);
        return;
      }
      if (workersOutcome.status === 422 && workersOutcome.code === "unknown_member") setBanner("unknownMember");
      else setBanner(problem(workersOutcome));
      setFailedOnce(true);
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }

  const submitLabel = editing ? t("submitEdit") : t("submitCreate");
  const tryAgainLabel = consoleForm("tryAgain");

  return (
    <form className={`${styles.form} ${styles.colWide}`} noValidate onSubmit={onSubmit}>
      {signedOut && <SignedOutBanner />}
      {!signedOut && banner && (
        <Banner tone="error">{banner === "unknownMember" ? services("unknownMember") : consoleForm(banner)}</Banner>
      )}
      {!editing && membersFailure && <Banner tone="error">{consoleForm(membersFailure)}</Banner>}

      <LangField
        heading={t("nameLabel")}
        values={name}
        onChange={(loc, value) => {
          markDirty();
          setName((current) => ({ ...current, [loc]: value }));
        }}
        businessLanguage={settings.language}
        placeholder={(language) => t("namePlaceholder", { language })}
        errorMessage={errors.name ? t("nameRequired") : undefined}
      />
      {!editing && <p className={styles.hint}>{t("nameHint")}</p>}

      {!editing && (
        <LangField
          heading={t("descriptionLabel")}
          headingHint={t("descriptionOptional")}
          values={description}
          onChange={(loc, value) => {
            markDirty();
            setDescription((current) => ({ ...current, [loc]: value }));
          }}
          businessLanguage={settings.language}
          requiredHint={t("descriptionOptional")}
          optionalHint={t("descriptionOptional")}
          placeholder={() => t("descriptionPlaceholder")}
          multiline
        />
      )}

      <div className={styles.row2}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor="service-price">
            {t("priceLabel")}
          </label>
          <div className={trailing ? `${styles.input} ${styles.prefixed} ${styles.trailing}` : `${styles.input} ${styles.prefixed}`}>
            <span className={styles.prefix} aria-hidden="true">
              {sign}
            </span>
            <input
              id="service-price"
              type="text"
              inputMode="decimal"
              autoComplete="off"
              value={price}
              onChange={(event) => {
                markDirty();
                setPrice(event.target.value);
              }}
              aria-invalid={errors.price ? true : undefined}
              aria-describedby={errors.price ? "service-price-error" : undefined}
            />
          </div>
          {errors.price ? (
            <FieldError id="service-price-error">{t("priceInvalid")}</FieldError>
          ) : (
            !editing && <p className={styles.hint}>{t("priceHint", { currency: currencyName })}</p>
          )}
        </div>
        <div className={styles.field}>
          <label className={styles.label} htmlFor="service-duration">
            {t("durationLabel")}
          </label>
          <div className={styles.input}>
            <input
              id="service-duration"
              type="number"
              inputMode="numeric"
              min={5}
              max={720}
              step={5}
              value={duration}
              onChange={(event) => {
                markDirty();
                setDuration(event.target.value);
              }}
              aria-invalid={errors.duration ? true : undefined}
              aria-describedby={errors.duration ? "service-duration-error" : undefined}
            />
          </div>
          {errors.duration ? (
            <FieldError id="service-duration-error">{t("durationRange")}</FieldError>
          ) : (
            <p className={styles.hint}>{editing ? t("durationHintEdit") : t("durationHintCreate")}</p>
          )}
        </div>
      </div>

      {!editing && (
        <fieldset className={styles.field} style={{ border: 0, margin: 0, padding: 0 }}>
          <legend className={styles.label} style={{ padding: 0 }}>
            {t("gapLegend")}
          </legend>
          <p className={styles.hint}>{t("gapHint")}</p>
          <div className={styles.choices} style={{ marginTop: "0.5rem" }}>
            <label className={styles.choice}>
              <input
                type="radio"
                name="gap"
                checked={gap === "default"}
                onChange={() => {
                  markDirty();
                  setGap("default");
                }}
              />
              <span>
                <b>{t("gapDefaultLabel")}</b>
                <em>{t("gapDefaultHint", { pct: settings.buffer_pct, n: defaultBuffer(Number(duration) || 0, settings.buffer_pct) })}</em>
              </span>
            </label>
            <label className={styles.choice}>
              <input
                type="radio"
                name="gap"
                checked={gap === "fixed"}
                onChange={() => {
                  markDirty();
                  setGap("fixed");
                }}
              />
              <span>
                <b>{t("gapFixedLabel")}</b>
                <em>{t("gapFixedHint")}</em>
              </span>
            </label>
          </div>
          {gap === "fixed" && (
            <div className={styles.field}>
              <label className={styles.srOnly} htmlFor="service-fixed-gap">
                {t("gapFixedNumberLabel")}
              </label>
              <div className={styles.input}>
                <input
                  id="service-fixed-gap"
                  type="number"
                  inputMode="numeric"
                  min={0}
                  max={240}
                  value={fixedGap}
                  onChange={(event) => {
                    markDirty();
                    setFixedGap(event.target.value);
                  }}
                  aria-invalid={errors.gap ? true : undefined}
                  aria-describedby={errors.gap ? "service-gap-error" : undefined}
                />
              </div>
              {errors.gap && <FieldError id="service-gap-error">{t("gapRange")}</FieldError>}
            </div>
          )}
        </fieldset>
      )}

      <fieldset className={styles.field} style={{ border: 0, margin: 0, padding: 0 }}>
        <legend className={styles.label} style={{ padding: 0 }}>
          {t("whoLegend")}
        </legend>
        {!editing && <p className={styles.hint}>{t("whoHint")}</p>}
        <div className={styles.choices} style={{ marginTop: "0.5rem" }}>
          {(members ?? []).map((member) => {
            const isSelf = member.member_id === session.member_id;
            const isChecked = checked.has(member.member_id);
            return (
              <label className={styles.choice} key={member.member_id}>
                <input
                  type="checkbox"
                  checked={isChecked}
                  onChange={() => {
                    markDirty();
                    setChecked((current) => {
                      const next = new Set(current);
                      if (next.has(member.member_id)) next.delete(member.member_id);
                      else next.add(member.member_id);
                      return next;
                    });
                  }}
                />
                <span>
                  <b className={member.display_name ? undefined : styles.unset}>{isSelf ? t("youLabel") : member.display_name || t("unnamedLabel")}</b>
                  <em>
                    {isSelf
                      ? t("youMeta", { name: member.display_name || member.email })
                      : member.display_name
                        ? t("memberMeta", { name: member.display_name })
                        : editing
                          ? t("unnamedMetaEdit", { email: member.email })
                          : t("unnamedMetaCreate", { email: member.email })}
                  </em>
                </span>
              </label>
            );
          })}
        </div>
      </fieldset>

      <div className={styles.actions}>
        <Submit busy={busy} busyLabel={t("submitBusy")}>
          {failedOnce ? tryAgainLabel : submitLabel}
        </Submit>
        <Link
          className={`${styles.button} ${styles.secondary}`}
          href="/services"
          aria-disabled={busy || undefined}
          onClick={(event) => {
            // aria-disabled alone doesn't stop a real <a> from navigating while a save is in
            // flight (design-r2.html:999): the click itself has to be swallowed too.
            if (busy) event.preventDefault();
          }}
        >
          {t("cancel")}
        </Link>
      </div>

      {editing && (
        <ArchiveZone
          service={props.service}
          locale={locale}
          businessLanguage={settings.language}
          onSignedOut={() => setSignedOut(true)}
        />
      )}
    </form>
  );
}

function ArchiveZone({
  service,
  locale,
  businessLanguage,
  onSignedOut,
}: {
  service: ServiceOut;
  locale: string;
  businessLanguage: Locale;
  // Shared with the enclosing form's own write-401 state, so archiving and saving never stack
  // two identical SignedOutBanners on the same page — only one signed-out state, one banner.
  onSignedOut: () => void;
}) {
  const t = useTranslations("Console.services.form");
  const consoleForm = useTranslations("Form");
  const { call } = useConsole();
  const router = useRouter();
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<ReturnType<typeof problem> | null>(null);
  const submitting = useRef(false);
  const startRef = useRef<HTMLButtonElement>(null);
  const yesRef = useRef<HTMLButtonElement>(null);
  // Tracks the confirming true<->false transition itself, not the currently focused element: the
  // form never unmounts this component, so "focus is still on the start button" would otherwise
  // never distinguish "just opened" from "already back", and every close ended up refocusing the
  // start button unconditionally.
  const wasConfirming = useRef(false);

  useEffect(() => {
    if (confirming && !wasConfirming.current) yesRef.current?.focus(); // design-r2.html:542
    else if (!confirming && wasConfirming.current) startRef.current?.focus(); // "Keep it" returns focus
    wasConfirming.current = confirming;
  }, [confirming]);

  const n = service.worker_ids.length;
  const bodyKey = n === 0 ? "archiveConfirmBodyNone" : n === 1 ? "archiveConfirmBodyOne" : "archiveConfirmBodyMany";

  async function archive() {
    if (submitting.current) return;
    submitting.current = true;
    setBusy(true);
    setBanner(null);
    try {
      const outcome = await call(() => servicesUpdate({ path: { service_id: service.id }, body: { archived: true } }), { write: true });
      if (outcome.status === 200) {
        router.push("/services");
        return;
      }
      if (outcome.status === 401) onSignedOut();
      else setBanner(problem(outcome));
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }

  const displayName = serviceName(service.name as NameMap, locale as Locale, businessLanguage);

  return (
    <>
      <div className={styles.divider} role="presentation" />
      {!confirming ? (
        <div className={styles.dangerZone}>
          <strong>{t("archiveZoneTitle")}</strong>
          <p>{t("archiveZoneBody")}</p>
          <button ref={startRef} className={`${styles.button} ${styles.danger}`} type="button" onClick={() => setConfirming(true)}>
            {t("archiveStart")}
          </button>
        </div>
      ) : (
        <div className={styles.dangerZone}>
          <strong>{t("archiveConfirmTitle", { name: displayName })}</strong>
          <p>{t(bodyKey, { n })}</p>
          {banner && <Banner tone="error">{consoleForm(banner)}</Banner>}
          <div className={styles.actions} style={{ margin: 0 }}>
            <button ref={yesRef} className={`${styles.button} ${styles.danger}`} type="button" aria-disabled={busy || undefined} onClick={archive}>
              {t("archiveConfirmYes")}
            </button>
            <button
              className={`${styles.button} ${styles.secondary}`}
              type="button"
              aria-disabled={busy || undefined}
              onClick={() => {
                if (busy) return;
                setConfirming(false);
              }}
            >
              {t("archiveConfirmKeep")}
            </button>
          </div>
        </div>
      )}
    </>
  );
}

export function NewService() {
  const t = useTranslations("Console.services.form");
  return (
    <>
      <BackLink />
      <Heading focus>{t("newHeading")}</Heading>
      <ServiceFormBody mode="create" />
    </>
  );
}

export function EditService({ serviceId }: { serviceId: string }) {
  const { call, settings } = useConsole();
  const locale = useLocale();
  const nav = useTranslations("Console.nav");
  const form = useTranslations("Form");
  const [service, setService] = useState<ServiceOut | null | undefined>(undefined);
  const [members, setMembers] = useState<MemberOut[] | null>(null);
  const [failure, setFailure] = useState<ReturnType<typeof problem> | null>(null);

  function load() {
    Promise.all([call(() => servicesRead({ path: { service_id: serviceId } })), call(() => membersList())]).then(
      ([serviceOutcome, membersOutcome]) => {
        if (serviceOutcome.status === 404) {
          setService(null);
          setFailure(null);
          return;
        }
        if (serviceOutcome.status === 200 && serviceOutcome.data && membersOutcome.status === 200 && membersOutcome.data) {
          setService(serviceOutcome.data);
          setMembers(membersOutcome.data);
          setFailure(null);
        } else {
          setFailure(problem(serviceOutcome.status !== 200 ? serviceOutcome : membersOutcome));
        }
      },
    );
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serviceId]);

  if (failure) {
    return (
      <>
        <BackLink />
        <Heading focus>{nav("services")}</Heading>
        <Banner tone="error">{form(failure)}</Banner>
        <button className={styles.textButton} type="button" onClick={load}>
          {form("tryAgain")}
        </button>
      </>
    );
  }

  if (service === undefined) {
    return (
      <>
        <BackLink />
        <Heading focus>{nav("services")}</Heading>
      </>
    );
  }

  if (service === null) {
    return (
      <>
        <BackLink />
        <Heading focus>{nav("services")}</Heading>
        <Banner tone="error">{form("unexpected")}</Banner>
      </>
    );
  }

  const heading = serviceName(service.name as NameMap, locale as Locale, settings.language);

  return (
    <>
      <BackLink />
      <Heading focus>{heading}</Heading>
      <ServiceFormBody mode="edit" service={service} members={members ?? []} />
    </>
  );
}

function BackLink() {
  const nav = useTranslations("Console.nav");
  return (
    <Link className={styles.back} href="/services">
      <svg aria-hidden="true" viewBox="0 0 12 12" width="12" height="12">
        <path d="M7.5 2.5l-3 3.5 3 3.5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      {nav("services")}
    </Link>
  );
}
