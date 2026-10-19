"use client";

import { useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState } from "react";

import { type DialOption, flagOf, matchDial } from "@/lib/dial-codes";

import { Chevron } from "../_ui/parts";
import styles from "../_ui/ui.module.css";

/** The phone's country-code picker: a flag + code button that opens a searchable list (WAI-ARIA
 * combobox on the search box). Renders the trigger and an absolutely positioned panel, so its parent
 * must be position: relative (`.phonePair`); the panel then spans that whole row. */
export function DialPicker({ options, value, onChange }: { options: DialOption[]; value: string; onChange: (region: string) => void }) {
  const t = useTranslations("BookingPage");
  const id = useId();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const trigger = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const search = useRef<HTMLInputElement>(null);

  const current = options.find((o) => o.region === value) ?? options[0];
  const shown = options.filter((o) => matchDial(o, query));
  const act = Math.min(active, shown.length - 1);
  const optId = (r: string) => `${id}-${r}`;

  const show = () => {
    setQuery("");
    setActive(Math.max(0, options.findIndex((o) => o.region === value)));
    setOpen(true);
  };
  const close = () => {
    setOpen(false);
    trigger.current?.focus();
  };
  const pick = (region: string) => {
    onChange(region);
    close();
  };

  useEffect(() => {
    if (!open) return;
    search.current?.focus();
    const away = (e: MouseEvent) => {
      if (!panel.current?.contains(e.target as Node) && !trigger.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, [open]);

  useEffect(() => {
    if (open && shown[act]) document.getElementById(optId(shown[act].region))?.scrollIntoView({ block: "nearest" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, act, query]);

  const onKey = (e: React.KeyboardEvent) => {
    const move = (to: number) => {
      e.preventDefault();
      setActive(Math.max(0, Math.min(shown.length - 1, to)));
    };
    if (e.key === "ArrowDown") move(act + 1);
    else if (e.key === "ArrowUp") move(act - 1);
    else if (e.key === "Home") move(0);
    else if (e.key === "End") move(shown.length - 1);
    else if (e.key === "Enter") {
      e.preventDefault(); // never submits the booking form
      if (shown[act]) pick(shown[act].region);
    } else if (e.key === "Escape") {
      e.preventDefault();
      close();
    } else if (e.key === "Tab") setOpen(false);
  };

  return (
    <>
      <div className={styles.input}>
        <button
          ref={trigger}
          id="bp-dial"
          type="button"
          className={styles.dialTrigger}
          aria-label={`${t("dialCode")}: ${current.name}`}
          aria-expanded={open}
          aria-controls={`${id}-panel`}
          onClick={() => (open ? setOpen(false) : show())}
        >
          <span aria-hidden="true">
            {`${flagOf(current.region)} +${current.dial}`}
          </span>
          <Chevron select />
        </button>
      </div>
      {open && (
        <div ref={panel} id={`${id}-panel`} className={styles.dialPanel}>
          <input
            ref={search}
            type="text"
            role="combobox"
            className={styles.dialSearch}
            aria-label={t("dialCode")}
            placeholder={t("dialSearch")}
            aria-expanded="true"
            aria-controls={`${id}-list`}
            aria-activedescendant={shown[act] ? optId(shown[act].region) : undefined}
            aria-autocomplete="list"
            autoComplete="off"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setActive(0);
            }}
            onKeyDown={onKey}
          />
          {shown.length === 0 ? (
            <p className={styles.dialEmpty} role="status">
              {t("noCountry")}
            </p>
          ) : (
            <ul id={`${id}-list`} role="listbox" aria-label={t("dialCode")} className={styles.dialList}>
              {shown.map((o, i) => (
                // Keyboard use is on the search box (aria-activedescendant); rows only take pointer picks.
                <li
                  key={o.region}
                  id={optId(o.region)}
                  role="option"
                  aria-selected={o.region === value}
                  className={`${styles.dialRow} ${i === act ? styles.dialRowActive : ""}`}
                  onMouseDown={(e) => e.preventDefault()}
                  onMouseMove={() => setActive(i)}
                  onClick={() => pick(o.region)}
                >
                  <span aria-hidden="true">{flagOf(o.region)}</span>
                  <span className={styles.dialName}>{o.name}</span>
                  <span className={styles.dialCode}>{`+${o.dial}`}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </>
  );
}
