import { getLocale } from "next-intl/server";

import { LEGAL } from "@/lib/legal";

import { Header } from "./header";
import styles from "./ui.module.css";

type Doc = "terms" | "privacy";

// getLocale(), never the raw param: i18n/request.ts answers an unknown locale with notFound().
const read = async (doc: Doc) => LEGAL[await getLocale()][doc];

export async function legalTitle(doc: Doc) {
  return { title: (await read(doc)).title };
}

/** /terms and /privacy (ZIF-147): a plain document under the public header. */
export async function LegalPage({ doc }: { doc: Doc }) {
  const { title, updated, sections } = await read(doc);
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
