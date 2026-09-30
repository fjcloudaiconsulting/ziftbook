"use client";

import { useTranslations } from "next-intl";
import { type MouseEvent, type ReactNode, useEffect, useRef } from "react";

import { Link } from "@/i18n/navigation";

import css from "./calendar.module.css";

/** The panel the detail's slot shows for a new booking, a move or blocked time: a Back link on a phone, a
 * close button on desktop, and a heading that takes focus when the panel opens. */
export function PanelFrame({ id, title, closeHref, onClose, children }: { id: string; title: string; closeHref: string; onClose(event: MouseEvent<HTMLAnchorElement>): void; children: ReactNode }) {
  const t = useTranslations("Console.calendar");
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    heading.current?.focus();
  }, []);
  return (
    <aside className={css.panel} aria-labelledby={id}>
      <Link className={css.backLink} href={closeHref} replace scroll={false} onClick={onClose}>
        <span aria-hidden="true">{"‹ "}</span>
        {t("back")}
      </Link>
      <div className={css.panelHead}>
        <h2 id={id} className={css.panelTitle} ref={heading} tabIndex={-1}>
          {title}
        </h2>
        <Link className={css.close} href={closeHref} replace scroll={false} aria-label={t("close")} onClick={onClose}>
          <span aria-hidden="true">{"✕"}</span>
        </Link>
      </div>
      {children}
    </aside>
  );
}
