"use client";

import { useTranslations } from "next-intl";

import styles from "../_ui/console.module.css";
import { Heading } from "../_ui/parts";

export function Clients() {
  const t = useTranslations("Console.clients");
  const nav = useTranslations("Console.nav");
  return (
    <>
      <Heading focus>{nav("clients")}</Heading>
      <div className={styles.placeholder}>
        <p>{t("comingSoon")}</p>
      </div>
    </>
  );
}
