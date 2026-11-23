"use client";

import { useLocale, useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState } from "react";

import { useRouter } from "@/i18n/navigation";

import {
  adminArchivePlan,
  adminChangeBilling,
  adminCreatePlan,
  adminListPlans,
  adminReadBilling,
  adminUpdatePlan,
  type BillingSettings,
  type PlanOut,
  type PlansOut,
} from "@/api-client";
import { needsSignIn } from "@/lib/admin";
import { type Feature, formFromValues, type PlanForm, valuesFromForm } from "@/lib/admin-plans";

import { Banner, Heading, type Outcome, problem, Screen, send } from "../../_ui/parts";
import styles from "../../_ui/ui.module.css";
import admin from "../admin.module.css";
import { FeatureField, focusField, SelectField, TextField } from "../feature-field";
import { BackLink } from "../parts";
import { WriteForm } from "../write-form";

type Mode = { form: "plan"; plan?: PlanOut } | { form: "archive"; plan: PlanOut } | { form: "settings" };
const LANGUAGES = ["en", "nl", "pt"] as const;

/** ZIF-78: the plans, the trial and the default plan. Every write asks for a reason and a fresh second factor. */
export function Plans() {
  const t = useTranslations("Admin");
  const form = useTranslations("Form");
  const locale = useLocale();
  const router = useRouter();
  const [answer, setAnswer] = useState<{ plans: Outcome<PlansOut>; settings: Outcome<BillingSettings> } | null>(null);
  const [reloads, setReloads] = useState(0);
  const [mode, setMode] = useState<Mode | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const doneLine = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let current = true;
    Promise.all([send(adminListPlans()), send(adminReadBilling())]).then(([plans, settings]) => {
      if (current) setAnswer({ plans, settings });
    });
    return () => {
      current = false;
    };
  }, [reloads]);

  const signIn = answer && (needsSignIn(answer.plans.status) || needsSignIn(answer.settings.status));
  useEffect(() => {
    if (signIn) router.replace("/admin/sign-in");
  }, [signIn, router]);

  const data = answer?.plans.data && answer.settings.data ? { plans: answer.plans.data, settings: answer.settings.data } : null;
  const failure = !answer || data || signIn ? null : form(problem(answer.plans.data ? answer.settings : answer.plans));

  useEffect(() => {
    if (done) doneLine.current?.focus();
  }, [done]);

  const name = (plan: Pick<PlanOut, "name">) => plan.name[locale] ?? plan.name.en;
  const byId = new Map(data?.plans.plans.map((plan) => [plan.id, plan]));
  const finish = (text: string) => {
    setMode(null);
    setDone(text);
    setReloads((n) => n + 1);
  };
  const open = (next: Mode) => {
    setDone(null);
    setMode(next);
  };

  return (
    <Screen wide>
      <BackLink href="/admin">{t("homeTitle")}</BackLink>
      <Heading>{t("plans.title")}</Heading>
      {failure && <Banner tone="error">{failure}</Banner>}
      {done && (
        <div ref={doneLine} tabIndex={-1} className={admin.quietFocus}>
          <Banner tone="info">{done}</Banner>
        </div>
      )}
      {data && (
        <>
          {mode?.form === "plan" && (
            <PlanEditor key={mode.plan?.id ?? "new"} plan={mode.plan} title={mode.plan ? t("plans.editTitle", { name: name(mode.plan) }) : t("plans.new")} features={data.plans.features} onDone={() => finish(t("plans.saved"))} onCancel={() => setMode(null)} />
          )}
          {mode?.form === "archive" && (
            <WriteForm
              title={t("plans.archiveTitle", { name: name(mode.plan) })}
              hint={t("plans.archiveBody")}
              submit={t("plans.archive")}
              danger
              run={(reason) => send(adminArchivePlan({ path: { plan_id: mode.plan.id }, body: { reason } }))}
              onDone={() => finish(t("plans.archived"))}
              onCancel={() => setMode(null)}
            />
          )}
          {mode?.form === "settings" && (
            <SettingsEditor settings={data.settings} plans={data.plans.plans} name={name} onDone={() => finish(t("plans.settingsSaved"))} onCancel={() => setMode(null)} />
          )}

          <section className={admin.section} aria-labelledby="settings">
            <h2 className={admin.sectionTitle} id="settings">
              {t("plans.settingsTitle")}
            </h2>
            <dl className={admin.facts}>
              {[
                [t("plans.defaultPlan"), byId.get(data.settings.default_plan_id) ? name(byId.get(data.settings.default_plan_id)!) : "—"],
                [t("plans.trialPlan"), data.settings.trial_plan_id ? (byId.get(data.settings.trial_plan_id) ? name(byId.get(data.settings.trial_plan_id)!) : "—") : t("plans.noTrial")],
                [t("plans.trialDays"), String(data.settings.trial_days)],
              ].map(([label, value]) => (
                <div key={label} className={admin.fact}>
                  <dt>{label}</dt>
                  <dd>{value}</dd>
                </div>
              ))}
            </dl>
            <button className={`${styles.button} ${styles.secondary}`} type="button" onClick={() => open({ form: "settings" })}>
              {t("plans.changeSettings")}
            </button>
          </section>

          <section className={admin.section} aria-labelledby="plans">
            <h2 className={admin.sectionTitle} id="plans">
              {t("plans.listTitle")}
            </h2>
            <ul className={styles.list}>
              {data.plans.plans.map((plan) => (
                <li key={plan.id} className={styles.rowStatic}>
                  <span className={styles.rowMain}>
                    <span className={styles.rowTitle}>{name(plan)}</span>
                    <span className={`${styles.rowMeta} ${admin.mono}`}>{[plan.slug, plan.stripe_price_id].filter(Boolean).join(" · ")}</span>
                  </span>
                  <span className={styles.pill}>{plan.archived_at ? t("plans.archivedPill") : plan.public ? t("plans.publicPill") : t("plans.privatePill")}</span>
                  <button className={`${styles.button} ${styles.secondary}`} type="button" onClick={() => open({ form: "plan", plan })}>
                    {t("plans.edit")}
                    <span className={styles.srOnly}> {name(plan)}</span>
                  </button>
                  {!plan.archived_at && (
                    <button className={`${styles.button} ${styles.secondary}`} type="button" onClick={() => open({ form: "archive", plan })}>
                      {t("plans.archive")}
                      <span className={styles.srOnly}> {name(plan)}</span>
                    </button>
                  )}
                </li>
              ))}
            </ul>
            <button className={`${styles.button} ${styles.primary}`} type="button" onClick={() => open({ form: "plan" })}>
              {t("plans.new")}
            </button>
          </section>
        </>
      )}
    </Screen>
  );
}

type Values = Record<string, boolean | number>;

function PlanEditor({ plan, title, features, onDone, onCancel }: { plan?: PlanOut; title: string; features: Feature[]; onDone(): void; onCancel(): void }) {
  const t = useTranslations("Admin");
  const id = useId();
  const [slug, setSlug] = useState(plan?.slug ?? "");
  const [names, setNames] = useState<Record<string, string>>({ en: plan?.name.en ?? "", nl: plan?.name.nl ?? "", pt: plan?.name.pt ?? "" });
  const [price, setPrice] = useState(plan?.stripe_price_id ?? "");
  const [isPublic, setPublic] = useState(plan?.public ?? false);
  const [values, setValues] = useState<PlanForm>(() => formFromValues(plan?.values ?? {}, features));
  const [invalid, setInvalid] = useState<string | null>(null); // the field refused: "slug", a language, or "f:<feature>"
  const fieldId = (field: string) => `${id}-${field}`;
  const mark = (field: string) => {
    setInvalid(field);
    focusField(fieldId(field));
  };
  const edit = <T,>(set: (value: T) => void) => (value: T) => {
    setInvalid(null);
    set(value);
  };
  const error = (field: string, text: string) => (invalid === field ? text : undefined);

  const check = () => {
    if (!slug.trim()) return mark("slug"), t("plans.required");
    const blank = LANGUAGES.find((language) => !names[language].trim());
    if (blank) return mark(blank), t("plans.required");
    const result = valuesFromForm(values, features);
    if ("error" in result) return mark(`f:${result.error}`), t("billing.errors.invalid_values", { key: result.error });
    return null;
  };

  return (
    <WriteForm
      title={title}
      submit={t("plans.save")}
      check={check}
      onInvalid={(key) => mark(`f:${key}`)}
      run={(reason) => {
        const { values: sent } = valuesFromForm(values, features) as { values: Values }; // check() passed first
        const body = {
          slug: slug.trim(),
          name: { en: names.en.trim(), nl: names.nl.trim(), pt: names.pt.trim() },
          stripe_price_id: price.trim() || null,
          values: sent,
          public: isPublic,
          reason,
        };
        return send(plan ? adminUpdatePlan({ path: { plan_id: plan.id }, body }) : adminCreatePlan({ body }));
      }}
      onDone={onDone}
      onCancel={onCancel}
    >
      <TextField id={fieldId("slug")} label={t("plans.slug")} hint={t("plans.slugHint")} error={error("slug", t("plans.required"))} value={slug} onChange={edit(setSlug)} mono maxLength={40} />
      {LANGUAGES.map((language) => (
        <TextField
          key={language}
          id={fieldId(language)}
          label={t("plans.nameIn", { language: t(`plans.language.${language}`) })}
          error={error(language, t("plans.required"))}
          value={names[language]}
          onChange={edit((value: string) => setNames({ ...names, [language]: value }))}
          maxLength={80}
        />
      ))}
      <TextField id={fieldId("price")} label={t("plans.price")} hint={t("plans.priceHint")} value={price} onChange={setPrice} mono maxLength={70} />
      <div className={styles.field}>
        <label className={admin.check}>
          <input type="checkbox" checked={isPublic} aria-describedby={`${id}-public`} onChange={(event) => setPublic(event.target.checked)} />
          <span>{t("plans.public")}</span>
        </label>
        <p className={styles.hint} id={`${id}-public`}>
          {t("plans.publicHint")}
        </p>
      </div>
      <fieldset className={styles.choices}>
        <legend className={styles.label}>{t("plans.featuresTitle")}</legend>
        {features.map((feature) => (
          <FeatureField
            key={feature.key}
            id={fieldId(`f:${feature.key}`)}
            feature={feature}
            value={values[feature.key]}
            error={error(`f:${feature.key}`, t("billing.errors.invalid_values", { key: feature.key }))}
            onChange={edit((value: boolean | string) => setValues({ ...values, [feature.key]: value }))}
          />
        ))}
      </fieldset>
    </WriteForm>
  );
}

function SettingsEditor({ settings, plans, name, onDone, onCancel }: { settings: BillingSettings; plans: PlanOut[]; name(plan: PlanOut): string; onDone(): void; onCancel(): void }) {
  const t = useTranslations("Admin");
  const id = useId();
  const [defaultPlan, setDefaultPlan] = useState(settings.default_plan_id);
  const [trialPlan, setTrialPlan] = useState(settings.trial_plan_id ?? "");
  const [days, setDays] = useState(String(settings.trial_days));
  const [invalid, setInvalid] = useState(false);
  // An archived plan can't be picked, but stays listed while it is the current choice.
  const options = plans.filter((plan) => !plan.archived_at || plan.id === settings.default_plan_id || plan.id === settings.trial_plan_id);
  const validDays = /^\d{1,2}$/.test(days.trim()) && Number(days) <= 90;

  return (
    <WriteForm
      title={t("plans.changeSettings")}
      submit={t("plans.save")}
      check={() => {
        if (validDays) return null;
        setInvalid(true);
        focusField(`${id}-days`);
        return t("plans.daysInvalid");
      }}
      run={(reason) => send(adminChangeBilling({ body: { default_plan_id: defaultPlan, trial_plan_id: trialPlan || null, trial_days: Number(days), reason } }))}
      onDone={onDone}
      onCancel={onCancel}
    >
      <SelectField id={`${id}-default`} label={t("plans.defaultPlan")} value={defaultPlan} onChange={setDefaultPlan}>
        {options.map((plan) => (
          <option key={plan.id} value={plan.id}>
            {name(plan)}
          </option>
        ))}
      </SelectField>
      <SelectField id={`${id}-trial`} label={t("plans.trialPlan")} value={trialPlan} onChange={setTrialPlan}>
        <option value="">{t("plans.noTrial")}</option>
        {options.map((plan) => (
          <option key={plan.id} value={plan.id}>
            {name(plan)}
          </option>
        ))}
      </SelectField>
      <TextField
        id={`${id}-days`}
        label={t("plans.trialDays")}
        hint={t("plans.trialDaysHint")}
        error={invalid ? t("plans.daysInvalid") : undefined}
        value={days}
        onChange={(value) => {
          setInvalid(false);
          setDays(value);
        }}
        numeric
        maxLength={2}
      />
    </WriteForm>
  );
}
