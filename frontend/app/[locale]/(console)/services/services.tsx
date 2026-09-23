"use client";

import { useLocale, useTranslations } from "next-intl";
import { type FormEvent, useEffect, useId, useMemo, useRef, useState } from "react";

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
import { defaultBuffer, type Locale, type NameMap, serviceBody, type ServiceForm, serviceName } from "@/lib/services";

import { useConsole } from "../../_ui/console";
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

function Row({ service, locale, showPeople }: { service: ServiceOut; locale: string; showPeople: boolean }) {
  const t = useTranslations("Console.services");
  const { settings } = useConsole();
  const name = serviceName(service.name as NameMap, locale as Locale, settings.language);
  const price = formatMoney(service.price.amount_minor, service.price.currency, locale);
  const n = service.worker_ids.length;

  return (
    <li>
      <Link href={`/services/${service.id}`} className={styles.rowLink}>
        <span className={styles.rowMain}>
          <span className={styles.rowTitle}>{name}</span>
          {showPeople && n > 0 && <span className={styles.rowMeta}>{t("rowMetaPeople", { minutes: service.duration_minutes, price, n })}</span>}
          {showPeople && n === 0 && (
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
          {!showPeople && <span className={styles.rowMeta}>{t("rowMeta", { minutes: service.duration_minutes, price })}</span>}
        </span>
        <svg className={styles.chev} aria-hidden="true" viewBox="0 0 12 12" width="12" height="12">
          <path d="M4.5 2.5l3 3.5-3 3.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </Link>
    </li>
  );
}

function ArchivedRow({ service, locale, onBroughtBack }: { service: ServiceOut; locale: string; onBroughtBack(service: ServiceOut): void }) {
  const t = useTranslations("Console.services");
  const form = useTranslations("Form");
  const { settings, call } = useConsole();
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<ReturnType<typeof problem> | null>(null);
  const name = serviceName(service.name as NameMap, locale as Locale, settings.language);
  const price = formatMoney(service.price.amount_minor, service.price.currency, locale);

  async function bringBack() {
    setBusy(true);
    setFailure(null);
    const outcome = await call(() => servicesUpdate({ path: { service_id: service.id }, body: { archived: false } }), { write: true });
    setBusy(false);
    if (outcome.status === 200 && outcome.data) onBroughtBack(outcome.data);
    else if (outcome.status !== 401) setFailure(problem(outcome));
  }

  return (
    <li>
      <div className={styles.rowStatic}>
        <span className={styles.rowMain}>
          <span className={styles.rowTitle}>{name}</span>
          <span className={styles.rowMeta}>{t("archivedMeta", { minutes: service.duration_minutes, price })}</span>
          {failure && <span className={styles.rowMeta}>{form(failure)}</span>}
        </span>
        <span className={styles.rowEnd}>
          <button className={`${styles.button} ${styles.secondary} ${styles.small}`} type="button" aria-disabled={busy || undefined} onClick={bringBack}>
            {t("bringBack")}
          </button>
        </span>
      </div>
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
      (services ?? [])
        .filter((s) => !s.archived)
        .sort((a, b) => collator.compare(serviceName(a.name as NameMap, locale as Locale, settings.language), serviceName(b.name as NameMap, locale as Locale, settings.language))),
    [services, collator, locale, settings.language],
  );
  const archived = useMemo(() => (services ?? []).filter((s) => s.archived), [services]);

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
            <Row key={service.id} service={service} locale={locale} showPeople={false} />
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
          <Row key={service.id} service={service} locale={locale} showPeople />
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
  values,
  onChange,
  businessLanguage,
  optionalHint,
  requiredHint,
  placeholder,
  multiline,
  errorMessage,
}: {
  values: Record<Locale, string>;
  onChange(locale: Locale, value: string): void;
  businessLanguage: Locale;
  optionalHint?: string;
  requiredHint: string;
  placeholder?(language: string): string;
  multiline?: boolean;
  errorMessage?: string;
}) {
  const t = useTranslations("Console.services.form");
  const [active, setActive] = useState<Locale>(businessLanguage);
  const idBase = useId();
  const errorId = `${idBase}-error`;

  return (
    <div className={styles.field}>
      <div className={styles.langTabs} role="tablist">
        {LOCALES.map((loc) => (
          <button
            key={loc}
            type="button"
            role="tab"
            aria-selected={active === loc}
            aria-controls={`${idBase}-${loc}`}
            data-filled={values[loc].trim() ? "yes" : "no"}
            className={styles.langTab}
            onClick={() => setActive(loc)}
          >
            <span className={styles.dot} aria-hidden="true" />
            {t(`languages.${loc}`)}
          </button>
        ))}
      </div>
      {LOCALES.map((loc) => (
        <div key={loc} id={`${idBase}-${loc}`} role="tabpanel" hidden={active !== loc} className={active === loc ? styles.langPanel + " is-on" : styles.langPanel}>
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
                {t("nameLangLabel", { language: t(`languages.${loc}`) })}{" "}
                <span className={styles.optional}>{loc === businessLanguage ? requiredHint : optionalHint}</span>
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
  const [busy, setBusy] = useState(false);
  const [failedOnce, setFailedOnce] = useState(false);

  useEffect(() => {
    if (props.mode !== "create") return;
    call(() => membersList()).then((outcome) => {
      if (outcome.status === 200 && outcome.data) setMembers(outcome.data);
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
    const form: ServiceForm = { name, description, price, duration, gap, fixedGap };
    const result = serviceBody(form, { businessLanguage: settings.language, mode: editing ? "edit" : "create" }, parseMoney);
    if ("errors" in result && result.errors) {
      setErrors(result.errors);
      return;
    }
    setErrors({});
    setBanner(null);
    setBusy(true);

    if (!editing) {
      const workerIds = [...checked];
      const outcome = await call(() => servicesCreate({ body: { ...result.body, worker_ids: workerIds } }), { write: true });
      setBusy(false);
      if (outcome.status === 201) {
        router.push("/services");
        return;
      }
      if (outcome.status === 401) return;
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
      setBusy(false);
      if (updateOutcome.status === 401) return;
      setBanner(problem(updateOutcome));
      setFailedOnce(true);
      return;
    }
    if (!workersChanged) {
      setBusy(false);
      router.push("/services");
      return;
    }
    const workersOutcome = await call(() => servicesReplaceWorkers({ path: { service_id: props.service.id }, body: [...after] }), { write: true });
    setBusy(false);
    if (workersOutcome.status === 200) {
      router.push("/services");
      return;
    }
    if (workersOutcome.status === 401) return;
    if (workersOutcome.status === 422 && workersOutcome.code === "unknown_member") setBanner("unknownMember");
    else setBanner(problem(workersOutcome));
    setFailedOnce(true);
  }

  const submitLabel = editing ? t("submitEdit") : t("submitCreate");
  const tryAgainLabel = consoleForm("tryAgain");

  return (
    <form className={`${styles.form} ${styles.colWide}`} noValidate onSubmit={onSubmit}>
      {banner && (
        <Banner tone="error">{banner === "unknownMember" ? services("unknownMember") : consoleForm(banner)}</Banner>
      )}

      <LangField
        values={name}
        onChange={(loc, value) => {
          markDirty();
          setName((current) => ({ ...current, [loc]: value }));
        }}
        businessLanguage={settings.language}
        requiredHint={t("nameLangRequired")}
        optionalHint={t("nameLangOptional")}
        placeholder={(language) => t("namePlaceholder", { language })}
        errorMessage={errors.name ? t("nameRequired", { language: t(`languages.${settings.language}`) }) : undefined}
      />
      {!editing && <p className={styles.hint}>{t("nameHint", { language: t(`languages.${settings.language}`) })}</p>}

      {!editing && (
        <LangField
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
          <div className={trailing ? `${styles.prefixed} ${styles.trailing}` : styles.prefixed}>
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
        <Link className={`${styles.button} ${styles.secondary}`} href="/services" aria-disabled={busy || undefined}>
          {t("cancel")}
        </Link>
      </div>

      {editing && <ArchiveZone service={props.service} />}
    </form>
  );
}

function ArchiveZone({ service }: { service: ServiceOut }) {
  const t = useTranslations("Console.services.form");
  const { call } = useConsole();
  const router = useRouter();
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const startRef = useRef<HTMLButtonElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (confirming) confirmRef.current?.focus();
    else if (startRef.current === document.activeElement) startRef.current?.focus();
  }, [confirming]);

  const n = service.worker_ids.length;
  const bodyKey = n === 0 ? "archiveConfirmBodyNone" : n === 1 ? "archiveConfirmBodyOne" : "archiveConfirmBodyMany";

  async function archive() {
    setBusy(true);
    const outcome = await call(() => servicesUpdate({ path: { service_id: service.id }, body: { archived: true } }), { write: true });
    setBusy(false);
    if (outcome.status === 200) router.push("/services");
  }

  const name = service.name as NameMap;
  const displayName = Object.values(name).find((v) => v) ?? "";

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
          <div className={styles.actions} style={{ margin: 0 }}>
            <button className={`${styles.button} ${styles.danger}`} type="button" aria-disabled={busy || undefined} onClick={archive}>
              {t("archiveConfirmYes")}
            </button>
            <button
              ref={confirmRef}
              className={`${styles.button} ${styles.secondary}`}
              type="button"
              onClick={() => setConfirming(false)}
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
  const { call } = useConsole();
  const nav = useTranslations("Console.nav");
  const form = useTranslations("Form");
  const [service, setService] = useState<ServiceOut | null | undefined>(undefined);
  const [members, setMembers] = useState<MemberOut[] | null>(null);
  const [failure, setFailure] = useState<ReturnType<typeof problem> | null>(null);

  useEffect(() => {
    Promise.all([call(() => servicesRead({ path: { service_id: serviceId } })), call(() => membersList())]).then(
      ([serviceOutcome, membersOutcome]) => {
        if (serviceOutcome.status === 404) {
          setService(null);
          return;
        }
        if (serviceOutcome.status === 200 && serviceOutcome.data && membersOutcome.status === 200 && membersOutcome.data) {
          setService(serviceOutcome.data);
          setMembers(membersOutcome.data);
        } else {
          setFailure(problem(serviceOutcome.status !== 200 ? serviceOutcome : membersOutcome));
        }
      },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serviceId]);

  if (failure) {
    return (
      <>
        <BackLink />
        <Heading focus>{nav("services")}</Heading>
        <Banner tone="error">{form(failure)}</Banner>
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

  const name = service.name as NameMap;
  const heading = Object.values(name).find((v) => v) ?? "";

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
