import { mailtoHref } from "@/lib/contact";

import styles from "./ui.module.css";

function MailIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 16 16" width="15" height="15">
      <rect x="2" y="3.5" width="12" height="9" rx="1.6" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <path d="M2.6 4.6 8 8.6l5.4-4" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
    </svg>
  );
}

/** The business's contact email (ZIF-99) under its name (`tight` right under a page heading): text the owner typed, so only ever text,
 * linked through mailtoHref. Callers render nothing when the business has no address. Not confirmed
 * yet (ZIF-232). */
export function ContactLine({ email, label, tight = false }: { email: string; label: string; tight?: boolean }) {
  return (
    <p className={`${styles.bizMeta} ${styles.contactLine} ${tight ? styles.metaTight : ""}`}>
      <MailIcon />
      <span className={styles.srOnly}>{label}</span>
      <a href={mailtoHref(email)}>{email}</a>
    </p>
  );
}
