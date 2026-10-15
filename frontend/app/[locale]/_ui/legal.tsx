import { LEGAL } from "@/lib/legal";
import type { Locale } from "@/lib/services";

import { Header } from "./header";
import styles from "./ui.module.css";

type Props = { params: Promise<{ locale: string }> };

export async function legalTitle({ params }: Props, doc: "terms" | "privacy") {
  return { title: LEGAL[(await params).locale as Locale][doc].title };
}

/** /terms and /privacy (ZIF-147): a plain document under the public header. */
export async function LegalPage({ params, doc }: Props & { doc: "terms" | "privacy" }) {
  const { title, updated, sections } = LEGAL[(await params).locale as Locale][doc];
  return (
    <>
      <Header />
      <main className={styles.screen}>
        <article className={`${styles.col} ${styles.colWide} ${styles.legal}`}>
          <h1 className={styles.heading}>{title}</h1>
          <p className={styles.hint}>{updated}</p>
          {sections.map((section) => (
            <section key={section.heading}>
              <h2>{section.heading}</h2>
              {section.body.map((block, i) =>
                Array.isArray(block) ? (
                  <ul key={i}>
                    {block.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                ) : (
                  <p key={i}>{block}</p>
                ),
              )}
            </section>
          ))}
        </article>
      </main>
    </>
  );
}
