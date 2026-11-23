"use client";

import { useLocale, useTranslations } from "next-intl";
import { type FormEvent, useId, useRef, useState } from "react";

import { Link, useRouter } from "@/i18n/navigation";
import { adminCreateBusiness } from "@/api-client";
import { byName, type Country, likelyCountry, signUpRequest } from "@/lib/account";
import type { Locale } from "@/lib/locale";

import { Banner, EmailField, Heading, problem, Screen, send, Submit } from "../../../_ui/parts";
import styles from "../../../_ui/ui.module.css";
import { BackLink } from "../../parts";
import { useSecondFactorRecheck } from "../../recheck";

export function NewBusiness() {
  const t = useTranslations("Admin");
  const signup = useTranslations("CompleteSignUp");
  const form = useTranslations("Form");
  const locale = useLocale() as Locale;
  const router = useRouter();
  const id = useId();
  const emailInput = useRef<HTMLInputElement>(null);
  const [name, setName] = useState("");
  const [businessName, setBusinessName] = useState("");
  const [country, setCountry] = useState<Country | "">(() => likelyCountry(locale));
  const [email, setEmail] = useState("");
  const [emailError, setEmailError] = useState<string>();
  const [message, setMessage] = useState<string>();
  const [created, setCreated] = useState<string>();
  const recheck = useSecondFactorRecheck({
    titleId: id,
    cancelLabel: t("billing.cancel"),
    onCancel: () => router.push("/admin/businesses"),
    async attempt() {
      setMessage(undefined);
      setEmailError(undefined);
      const request = signUpRequest(name, businessName, country);
      if ("errors" in request) return { status: 422 };
      const outcome = await send(adminCreateBusiness({ body: { ...request.body, email: email.trim(), locale } }));
      if (outcome.status === 201 && outcome.data) setCreated(outcome.data.id);
      return outcome;
    },
    onStep(step, outcome) {
      if (step === "done") return;
      if (outcome.code === "account_exists") {
        setEmailError(t("createBusiness.exists"));
        requestAnimationFrame(() => emailInput.current?.focus());
      } else setMessage(form(problem(outcome)));
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    if (recheck.busy) return;
    const request = signUpRequest(name, businessName, country);
    if ("errors" in request) {
      const error = request.errors.personName ?? request.errors.name ?? request.errors.country;
      if (error) setMessage(signup(error));
      return;
    }
    recheck.start();
  }

  if (created) return (
    <Screen>
      <Heading focus>{t("createBusiness.done")}</Heading>
      <p className={styles.lede}>{t("createBusiness.sent")}</p>
      <Link className={`${styles.button} ${styles.primary}`} href={`/admin/businesses/${created}`}>
        {t("createBusiness.view")}
      </Link>
    </Screen>
  );

  return (
    <Screen>
      <BackLink href="/admin/businesses">{t("businessesTitle")}</BackLink>
      <Heading>{t("createBusiness.title")}</Heading>
      <p className={styles.lede}>{t("createBusiness.lede")}</p>
      {recheck.banners}
      {message && <Banner tone="error">{message}</Banner>}
      {recheck.phase === "code" ? recheck.codeForm : (
        <form className={styles.form} onSubmit={submit}>
          <div className={styles.field}>
            <label className={styles.label} htmlFor={`${id}-business`}>{signup("businessName")}</label>
            <div className={styles.input}>
              <input id={`${id}-business`} name="business_name" autoComplete="off" required maxLength={100} value={businessName} onChange={(event) => setBusinessName(event.target.value)} />
            </div>
          </div>
          <div className={styles.field}>
            <label className={styles.label} htmlFor={`${id}-country`}>{signup("country")}</label>
            <div className={styles.input}>
              <select id={`${id}-country`} name="country" required value={country} onChange={(event) => setCountry(event.target.value as Country)} aria-describedby={`${id}-country-hint`}>
                <option value="" disabled>{signup("countryPlaceholder")}</option>
                {byName(locale, (c) => signup(`countries.${c}`)).map((c) => <option key={c} value={c}>{signup(`countries.${c}`)}</option>)}
              </select>
            </div>
            <p className={styles.hint} id={`${id}-country-hint`}>{signup("countryHint")}</p>
          </div>
          <div className={styles.field}>
            <label className={styles.label} htmlFor={`${id}-owner`}>{t("createBusiness.ownerName")}</label>
            <div className={styles.input}>
              <input id={`${id}-owner`} name="name" autoComplete="off" required maxLength={60} value={name} onChange={(event) => setName(event.target.value)} />
            </div>
          </div>
          <EmailField value={email} onChange={(value) => { setEmail(value); setEmailError(undefined); }} autoComplete="off" error={emailError} inputRef={emailInput} />
          <Submit busy={recheck.busy} busyLabel={form("sending")}>{t("createBusiness.title")}</Submit>
        </form>
      )}
    </Screen>
  );
}
