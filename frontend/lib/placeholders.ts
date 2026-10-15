// The picture a service without a photo shows (ZIF-133). Kept free of React and of sibling imports,
// as lib/services.ts, so node --test runs it directly.
export type Placeholder = "generic" | "nails" | "hair" | "beauty" | "massage";
export const PLACEHOLDERS: Placeholder[] = ["generic", "nails", "hair", "beauty", "massage"];

// Folded (lower case, no accents). Each matches at a word start; a trailing $ means a whole word only
// (a short word that starts unrelated ones: "gel" in "gelaatsbehandeling", a facial). Migration
// 0040 holds a frozen copy for its one-time backfill.
const WORDS: [Placeholder, string[]][] = [
  ["nails", ["nail", "mani", "pedi", "gel$", "gelnagel", "polish", "acryl", "shellac", "nagel", "gellak", "lak$", "unha", "esmalte", "alongamento"]],
  [
    "hair",
    ["hair", "haircut", "trim", "colour", "color", "blow", "highlight", "beard", "haar", "knip", "kleur", "fohn", "baard", "cabelo", "corte", "escova", "coloracao", "mechas", "barba"],
  ],
  [
    "beauty",
    ["brow", "lash", "facial", "face", "make-up", "makeup", "wax", "wenkbrauw", "wimper", "gezicht", "gelaat", "harsen", "sobrancelha", "cilio", "limpeza de pele", "rosto", "maquiagem", "depilacao"],
  ],
  ["massage", ["massage", "massagem"]],
];

// A word start, as Postgres's \m in migration 0040: not preceded by a letter, a digit or _; with
// a trailing $, also not followed by one (\M).
function wordAt(word: string): RegExp {
  const whole = word.endsWith("$");
  return new RegExp(`(?<![\\p{L}\\p{N}_])${whole ? word.slice(0, -1) : word}${whole ? "(?![\\p{L}\\p{N}_])" : ""}`, "u");
}

function fold(text: string): string {
  return text.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase();
}

/** The first kind, in WORDS order, with a word starting a word in any language of the name; else
 * generic. Kind order, not language order: "Hair massage" is hair. */
export function placeholderFor(name: Partial<Record<string, string>>): Placeholder {
  const texts = Object.values(name).filter((v): v is string => Boolean(v)).map(fold);
  for (const [kind, words] of WORDS) {
    if (words.some((word) => texts.some((text) => wordAt(word).test(text)))) return kind;
  }
  return "generic";
}
