// The picture a service without a photo shows (ZIF-133). Kept free of React and of sibling imports,
// as lib/services.ts, so node --test runs it directly.
export type Placeholder = "generic" | "nails" | "hair" | "beauty" | "massage";
export const PLACEHOLDERS: Placeholder[] = ["generic", "nails", "hair", "beauty", "massage"];

// Folded (lower case, no accents). Migration 0040 holds a frozen copy for its one-time backfill.
const WORDS: [Placeholder, string[]][] = [
  ["nails", ["nail", "mani", "pedi", "gel", "polish", "acryl", "shellac", "nagel", "gellak", "lak", "unha", "esmalte", "alongamento"]],
  [
    "hair",
    ["hair", "haircut", "trim", "colour", "color", "blow", "highlight", "beard", "haar", "knip", "kleur", "fohn", "baard", "cabelo", "corte", "escova", "coloracao", "mechas", "barba"],
  ],
  [
    "beauty",
    ["brow", "lash", "facial", "make-up", "makeup", "wax", "wenkbrauw", "wimper", "gezicht", "harsen", "sobrancelha", "cilio", "limpeza de pele", "maquiagem", "depilacao"],
  ],
  ["massage", ["massage", "massagem"]],
];

function fold(text: string): string {
  return text.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase();
}

/** The first kind, in WORDS order, with a word starting a word in any language of the name; else
 * generic. Kind order, not language order: "Hair massage" is hair. */
export function placeholderFor(name: Partial<Record<string, string>>): Placeholder {
  const texts = Object.values(name).filter((v): v is string => Boolean(v)).map(fold);
  for (const [kind, words] of WORDS) {
    // A word start, as Postgres's \m in migration 0040: not preceded by a letter, a digit or _.
    if (words.some((word) => texts.some((text) => new RegExp(`(?<![\\p{L}\\p{N}_])${word}`, "u").test(text)))) return kind;
  }
  return "generic";
}
