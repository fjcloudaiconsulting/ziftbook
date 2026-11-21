"use client";

import { useLocale, useTranslations } from "next-intl";
import { useEffect, useId, useState } from "react";

import { useRouter } from "@/i18n/navigation";

import {
  adminEndGrant,
  adminGrantPlan,
  adminListPlans,
  adminOfferPlan,
  adminReadBusinessBilling,
  adminRemoveOverride,
  adminSetOverride,
  adminWithdrawOffer,
  type BusinessBilling,
  type PlansOut,
} from "@/api-client";
import { needsSignIn, shortDate } from "@/lib/admin";
import { formatPercent, valuesFromForm } from "@/lib/admin-plans";
import { dateLocale } from "@/lib/console";

import { Banner, type Outcome, problem, send } from "../../../_ui/parts";
import styles from "../../../_ui/ui.module.css";
import admin from "../../admin.module.css";
import { FeatureField, focusField, SelectField, TextField } from "../../feature-field";
import { WriteForm } from "../../write-form";

type Mode = { form: "override" } | { form: "removeOverride"; key: string } | { form: "offer" } | { form: "withdraw"; planId: string } | { form: "grant" } | { form: "endGrant" };

const STATUSES = ["trialing", "active", "past_due", "canceled", "unpaid", "incomplete", "incomplete_expired", "paused"] as const;

// A datetime-local field is the admin's own clock; the API takes an instant. Empty: no end.
const instant = (local: string) => (local ? new Date(local).toISOString() : null);

/** ZIF-78: this business's plan, subscription, overrides, offers and free plan, with the writes for each. */
export function BusinessBilling({ tenantId }: { tenantId: string }) {
  const t = useTranslations("Admin");
  const form = useTranslations("Form");
  const locale = useLocale();
  const router = useRouter();
  const [answer, setAnswer] = useState<{ billing: Outcome<BusinessBilling>; plans: Outcome<PlansOut> } | null>(null);
  const [reloads, setReloads] = useState(0);
  const [mode, setMode] = useState<Mode | null>(null);
  const [done, setDone] = useState<string | null>(null);

  // Every business billing GET is recorded in the platform audit log, so it runs on the id and after a change only
  // (not on a new router or translator).
  useEffect(() => {
    let current = true;
    Promise.all([send(adminReadBusinessBilling({ path: { tenant_id: tenantId } })), send(adminListPlans())]).then(([billing, plans]) => {
      if (current) setAnswer({ billing, plans });
    });
    return () => {
      current = false;
    };
  }, [tenantId, reloads]);

  const signIn = answer && (needsSignIn(answer.billing.status) || needsSignIn(answer.plans.status));
  useEffect(() => {
    if (signIn) router.replace("/admin/sign-in");
  }, [signIn, router]);

  const data = answer?.billing.data && answer.plans.data ? { billing: answer.billing.data, plans: answer.plans.data } : null;
  const failure = !answer || data || signIn ? null : form(problem(answer.billing.data ? answer.plans : answer.billing));

  if (!data) return failure ? <Banner tone="error">{failure}</Banner> : null;

  const { billing, plans } = data;
  const day = new Intl.DateTimeFormat(dateLocale(locale), { day: "numeric", month: "short", year: "numeric" });
  const planById = new Map(plans.plans.map((plan) => [plan.id, plan]));
  const planName = (id: string | null) => {
    const plan = id ? planById.get(id) : undefined;
    return plan ? (plan.name[locale] ?? plan.name.en) : "—";
  };
  const feature = (key: string) => plans.features.find((f) => f.key === key);
  const shown = (key: string, value: boolean | number) => (typeof value === "boolean" ? (value ? t("yes") : t("no")) : feature(key)?.kind === "percent" ? formatPercent(value, locale) : String(value));
  // A status this build has no words for shows as Stripe names it.
  const status = (value: string) => (STATUSES.find((known) => known === value) ? t(`billing.status.${value as (typeof STATUSES)[number]}`) : value);
  const effective = billing.effective;
  const sellable = plans.plans.filter((plan) => !plan.archived_at);
  const finish = (text: string) => {
    setMode(null);
    setDone(text);
    setReloads((n) => n + 1);
  };
  const open = (next: Mode) => {
    setDone(null);
    setMode(next);
  };
  const cancel = () => setMode(null);
  const sourceDate =
    effective.source === "trial" ? [t("billing.trialEnds"), effective.trial_ends_at] : effective.source === "grant" ? [t("billing.grantEnds"), effective.grant_ends_at] : effective.source === "subscription" ? [t("billing.periodEnds"), effective.period_end] : null;

  return (
    <section className={admin.section} aria-labelledby="billing">
      <h2 className={admin.sectionTitle} id="billing">
        {t("billing.title")}
      </h2>
      {failure && <Banner tone="error">{failure}</Banner>}
      {done && <Banner tone="info">{done}</Banner>}
      <dl className={admin.facts}>
        {[
          [t("billing.plan"), `${planName(effective.plan_id)} · ${t(`billing.source.${effective.source}`)}`],
          ...(sourceDate ? [[sourceDate[0], shortDate(sourceDate[1], day)]] : []),
          ...(effective.source === "subscription" && effective.cancel_at_period_end ? [[t("billing.cancelling"), t("yes")]] : []),
          ...(billing.subscription ? [[t("billing.subscription"), `${planName(billing.subscription.plan_id)} · ${status(billing.subscription.status)}`]] : []),
        ].map(([label, value]) => (
          <div key={label} className={admin.fact}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>

      <h3 className={admin.sectionTitle}>{t("billing.overrides")}</h3>
      {billing.overrides.length === 0 && <p className={styles.hint}>{t("billing.noOverrides")}</p>}
      <ul className={styles.list}>
        {billing.overrides.map((o) => (
          <li key={o.key} className={styles.rowStatic}>
            <span className={styles.rowMain}>
              <span className={`${styles.rowTitle} ${admin.mono}`}>
                {`${o.key}: ${shown(o.key, o.value)}`}
              </span>
              <span className={`${styles.rowMeta} ${admin.reason}`}>
                {[o.expires_at ? t("billing.until", { when: shortDate(o.expires_at, day) }) : t("billing.untilRemoved"), o.reason].join(" · ")}
              </span>
            </span>
            <button className={`${styles.button} ${styles.secondary}`} type="button" onClick={() => open({ form: "removeOverride", key: o.key })}>
              {t("billing.remove")}
              <span className={styles.srOnly}> {o.key}</span>
            </button>
          </li>
        ))}
      </ul>
      {mode?.form === "override" && <OverrideForm tenantId={tenantId} plans={plans} onDone={() => finish(t("billing.overrideSet"))} onCancel={cancel} />}
      {mode?.form === "removeOverride" && (
        <WriteForm
          title={t("billing.removeOverrideTitle", { key: mode.key })}
          submit={t("billing.remove")}
          danger
          run={(reason) => send(adminRemoveOverride({ path: { tenant_id: tenantId, key: mode.key }, body: { reason } }))}
          onDone={() => finish(t("billing.overrideRemoved"))}
          onCancel={cancel}
        />
      )}
      <button className={`${styles.button} ${styles.secondary}`} type="button" onClick={() => open({ form: "override" })}>
        {t("billing.addOverride")}
      </button>

      <h3 className={admin.sectionTitle}>{t("billing.offers")}</h3>
      {billing.offers.length === 0 && <p className={styles.hint}>{t("billing.noOffers")}</p>}
      <ul className={styles.list}>
        {billing.offers.map((o) => (
          <li key={o.plan_id} className={styles.rowStatic}>
            <span className={styles.rowMain}>
              <span className={styles.rowTitle}>{planName(o.plan_id)}</span>
              <span className={`${styles.rowMeta} ${admin.reason}`}>{[o.coupon_id ? t("billing.coupon", { id: o.coupon_id }) : t("billing.noCoupon"), o.reason].join(" · ")}</span>
            </span>
            <button className={`${styles.button} ${styles.secondary}`} type="button" onClick={() => open({ form: "withdraw", planId: o.plan_id })}>
              {t("billing.withdraw")}
              <span className={styles.srOnly}> {planName(o.plan_id)}</span>
            </button>
          </li>
        ))}
      </ul>
      {mode?.form === "offer" && <OfferForm tenantId={tenantId} plans={sellable} planName={planName} onDone={() => finish(t("billing.offerSet"))} onCancel={cancel} />}
      {mode?.form === "withdraw" && (
        <WriteForm
          title={t("billing.withdrawTitle", { plan: planName(mode.planId) })}
          hint={t("billing.withdrawBody")}
          submit={t("billing.withdraw")}
          danger
          run={(reason) => send(adminWithdrawOffer({ path: { tenant_id: tenantId, plan_id: mode.planId }, body: { reason } }))}
          onDone={() => finish(t("billing.offerWithdrawn"))}
          onCancel={cancel}
        />
      )}
      <button className={`${styles.button} ${styles.secondary}`} type="button" onClick={() => open({ form: "offer" })}>
        {t("billing.addOffer")}
      </button>

      <h3 className={admin.sectionTitle}>{t("billing.grant")}</h3>
      {billing.grant ? (
        <ul className={styles.list}>
          <li className={styles.rowStatic}>
            <span className={styles.rowMain}>
              <span className={styles.rowTitle}>{planName(billing.grant.plan_id)}</span>
              <span className={`${styles.rowMeta} ${admin.reason}`}>
                {[billing.grant.ends_at ? t("billing.until", { when: shortDate(billing.grant.ends_at, day) }) : t("billing.untilEnded"), billing.grant.reason].join(" · ")}
              </span>
            </span>
            <button className={`${styles.button} ${styles.secondary}`} type="button" onClick={() => open({ form: "endGrant" })}>
              {t("billing.endGrant")}
            </button>
          </li>
        </ul>
      ) : (
        <p className={styles.hint}>{t("billing.noGrant")}</p>
      )}
      {mode?.form === "grant" && <GrantForm tenantId={tenantId} plans={sellable} planName={planName} replacing={billing.grant !== null} onDone={() => finish(t("billing.grantSet"))} onCancel={cancel} />}
      {mode?.form === "endGrant" && (
        <WriteForm
          title={t("billing.endGrantTitle")}
          hint={t("billing.endGrantBody")}
          submit={t("billing.endGrant")}
          danger
          run={(reason) => send(adminEndGrant({ path: { tenant_id: tenantId }, body: { reason } }))}
          onDone={() => finish(t("billing.grantEnded"))}
          onCancel={cancel}
        />
      )}
      <button className={`${styles.button} ${styles.secondary}`} type="button" onClick={() => open({ form: "grant" })}>
        {billing.grant ? t("billing.changeGrant") : t("billing.addGrant")}
      </button>
    </section>
  );
}

function OverrideForm({ tenantId, plans, onDone, onCancel }: { tenantId: string; plans: PlansOut; onDone(): void; onCancel(): void }) {
  const t = useTranslations("Admin");
  const id = useId();
  const [key, setKey] = useState(plans.features[0].key);
  const [value, setValue] = useState<boolean | string>(plans.features[0].kind === "flag" ? false : "");
  const [expires, setExpires] = useState("");
  const [invalid, setInvalid] = useState<"value" | "expires" | null>(null);
  const chosen = plans.features.find((f) => f.key === key) ?? plans.features[0];
  const typed = () => valuesFromForm({ [chosen.key]: value }, [chosen]);
  const mark = (field: "value" | "expires") => {
    setInvalid(field);
    focusField(`${id}-${field}`);
  };

  return (
    <WriteForm
      title={t("billing.addOverride")}
      submit={t("billing.save")}
      onInvalid={() => mark("value")}
      check={() => {
        if ("error" in typed()) {
          mark("value");
          return t("billing.errors.invalid_values", { key });
        }
        if (expires && new Date(expires) <= new Date()) {
          mark("expires");
          return t("billing.mustBeFuture");
        }
        return null;
      }}
      run={(reason) => {
        const { values } = typed() as { values: Record<string, boolean | number> }; // check() passed first
        return send(adminSetOverride({ path: { tenant_id: tenantId, key }, body: { value: values[key], expires_at: instant(expires), reason } }));
      }}
      onDone={onDone}
      onCancel={onCancel}
    >
      <SelectField
        id={`${id}-key`}
        label={t("billing.feature")}
        hint={chosen.description}
        value={key}
        onChange={(next) => {
          setKey(next);
          setInvalid(null);
          setValue(plans.features.find((f) => f.key === next)?.kind === "flag" ? false : "");
        }}
      >
        {plans.features.map((f) => (
          <option key={f.key} value={f.key}>
            {f.key}
          </option>
        ))}
      </SelectField>
      <FeatureField
        key={chosen.key}
        id={`${id}-value`}
        feature={{ ...chosen, description: "" }}
        value={value}
        error={invalid === "value" ? t("billing.errors.invalid_values", { key }) : undefined}
        onChange={(next) => {
          setInvalid(null);
          setValue(next);
        }}
      />
      <TextField
        id={`${id}-expires`}
        type="datetime-local"
        label={t("billing.expires")}
        hint={t("billing.expiresHint")}
        error={invalid === "expires" ? t("billing.mustBeFuture") : undefined}
        value={expires}
        onChange={(next) => {
          setInvalid(null);
          setExpires(next);
        }}
      />
    </WriteForm>
  );
}

function OfferForm({ tenantId, plans, planName, onDone, onCancel }: { tenantId: string; plans: PlansOut["plans"]; planName(id: string): string; onDone(): void; onCancel(): void }) {
  const t = useTranslations("Admin");
  const id = useId();
  const [planId, setPlanId] = useState(plans[0]?.id ?? "");
  const [coupon, setCoupon] = useState("");
  return (
    <WriteForm
      title={t("billing.addOffer")}
      submit={t("billing.save")}
      check={() => (planId ? null : t("billing.choosePlan"))}
      run={(reason) => send(adminOfferPlan({ path: { tenant_id: tenantId, plan_id: planId }, body: { coupon_id: coupon.trim() || null, reason } }))}
      onDone={onDone}
      onCancel={onCancel}
    >
      <SelectField id={`${id}-plan`} label={t("billing.planLabel")} value={planId} onChange={setPlanId}>
        {plans.map((plan) => (
          <option key={plan.id} value={plan.id}>
            {planName(plan.id)}
          </option>
        ))}
      </SelectField>
      <TextField id={`${id}-coupon`} label={t("billing.couponLabel")} hint={t("billing.couponHint")} value={coupon} onChange={setCoupon} mono maxLength={64} />
    </WriteForm>
  );
}

function GrantForm({ tenantId, plans, planName, replacing, onDone, onCancel }: { tenantId: string; plans: PlansOut["plans"]; planName(id: string): string; replacing: boolean; onDone(): void; onCancel(): void }) {
  const t = useTranslations("Admin");
  const id = useId();
  const [planId, setPlanId] = useState(plans[0]?.id ?? "");
  const [ends, setEnds] = useState("");
  const [invalid, setInvalid] = useState(false);
  return (
    <WriteForm
      title={replacing ? t("billing.changeGrant") : t("billing.addGrant")}
      hint={replacing ? t("billing.replaceGrantBody") : undefined}
      submit={t("billing.save")}
      check={() => {
        if (!planId) return t("billing.choosePlan");
        if (!ends || new Date(ends) > new Date()) return null;
        setInvalid(true);
        focusField(`${id}-ends`);
        return t("billing.mustBeFuture");
      }}
      run={(reason) => send(adminGrantPlan({ path: { tenant_id: tenantId }, body: { plan_id: planId, ends_at: instant(ends), reason } }))}
      onDone={onDone}
      onCancel={onCancel}
    >
      <SelectField id={`${id}-plan`} label={t("billing.planLabel")} value={planId} onChange={setPlanId}>
        {plans.map((plan) => (
          <option key={plan.id} value={plan.id}>
            {planName(plan.id)}
          </option>
        ))}
      </SelectField>
      <TextField id={`${id}-ends`} type="datetime-local" label={t("billing.endsLabel")}
        hint={t("billing.endsHint")}
        error={invalid ? t("billing.mustBeFuture") : undefined}
        value={ends}
        onChange={(next) => {
          setInvalid(false);
          setEnds(next);
        }}
      />
    </WriteForm>
  );
}
