"use client";

import type { ReactNode } from "react";

import type { Feature } from "@/lib/admin-plans";

import { FieldError } from "../_ui/parts";
import styles from "../_ui/ui.module.css";
import admin from "./admin.module.css";

/** Moves focus to a field by its id, after the render that shows its error. */
export function focusField(id: string) {
  requestAnimationFrame(() => document.getElementById(id)?.focus());
}

function Below({ id, hint, error }: { id: string; hint?: string; error?: string }) {
  return (
    <>
      {hint && (
        <p className={styles.hint} id={`${id}-hint`}>
          {hint}
        </p>
      )}
      {error && <FieldError id={`${id}-error`}>{error}</FieldError>}
    </>
  );
}

const describedBy = (id: string, hint?: string, error?: string) => [hint && `${id}-hint`, error && `${id}-error`].filter(Boolean).join(" ") || undefined;

/** One registry feature as a control by its kind (ZIF-78): a checkbox, a whole number, or a percent with up to two
 * decimals. The registry's description is its hint. */
export function FeatureField({ id, feature, value, error, onChange }: { id: string; feature: Feature; value: boolean | string; error?: string; onChange(value: boolean | string): void }) {
  if (feature.kind === "flag") {
    return (
      <div className={styles.field}>
        <label className={admin.check}>
          <input id={id} type="checkbox" checked={value === true} aria-describedby={describedBy(id, feature.description)} onChange={(event) => onChange(event.target.checked)} />
          <span className={admin.mono}>{feature.key}</span>
        </label>
        <Below id={id} hint={feature.description} />
      </div>
    );
  }
  return (
    <TextField
      id={id}
      label={feature.kind === "percent" ? `${feature.key} (%)` : feature.key}
      labelClass={admin.mono}
      hint={feature.description}
      value={typeof value === "string" ? value : ""}
      error={error}
      mono
      numeric={feature.kind === "count"}
      decimal={feature.kind === "percent"}
      onChange={onChange}
    />
  );
}

export function TextField({
  id,
  label,
  labelClass,
  hint,
  error,
  value,
  onChange,
  mono,
  numeric,
  decimal,
  maxLength,
  type,
}: {
  id: string;
  label: string;
  labelClass?: string;
  hint?: string;
  error?: string;
  value: string;
  onChange(value: string): void;
  mono?: boolean;
  numeric?: boolean;
  decimal?: boolean;
  maxLength?: number;
  type?: string;
}) {
  return (
    <div className={styles.field}>
      <label className={`${styles.label} ${labelClass ?? ""}`} htmlFor={id}>
        {label}
      </label>
      <div className={styles.input}>
        <input
          id={id}
          type={type}
          className={mono ? admin.mono : undefined}
          inputMode={numeric ? "numeric" : decimal ? "decimal" : undefined}
          autoComplete="off"
          spellCheck={false}
          maxLength={maxLength}
          value={value}
          aria-invalid={error ? true : undefined}
          aria-describedby={describedBy(id, hint, error)}
          onChange={(event) => onChange(event.target.value)}
        />
      </div>
      <Below id={id} hint={hint} error={error} />
    </div>
  );
}

export function SelectField({ id, label, hint, value, onChange, children }: { id: string; label: string; hint?: string; value: string; onChange(value: string): void; children: ReactNode }) {
  return (
    <div className={styles.field}>
      <label className={styles.label} htmlFor={id}>
        {label}
      </label>
      <div className={styles.input}>
        <select id={id} value={value} aria-describedby={describedBy(id, hint)} onChange={(event) => onChange(event.target.value)}>
          {children}
        </select>
        <svg aria-hidden="true" className={styles.selectChevron} viewBox="0 0 12 12" width="12" height="12">
          <path d="M3 4.5l3 3 3-3" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </div>
      <Below id={id} hint={hint} />
    </div>
  );
}
