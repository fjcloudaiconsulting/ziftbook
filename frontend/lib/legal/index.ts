/** The Terms and Privacy pages (ZIF-147), one module per language. Plain data, rendered on the
 * server only: kept out of the message catalogs, which every page ships to the browser. */
import { en } from "./en.ts";
import { nl } from "./nl.ts";
import { pt } from "./pt.ts";

/** A paragraph, or a bulleted list. */
export type Block = string | string[];
export type Section = { heading: string; body: Block[] };
export type Doc = { title: string; updated: string; sections: Section[] };
export type Legal = { terms: Doc; privacy: Doc };

export const LEGAL: Record<"en" | "nl" | "pt", Legal> = { en, nl, pt };
