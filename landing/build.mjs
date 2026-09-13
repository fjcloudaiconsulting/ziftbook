// Renders template.html once per locale into public/. No dependencies.
import { cpSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";

const SITE = "https://ziftbook.com";
const LOCALES = [
  { id: "en", path: "/en/" },
  { id: "nl", path: "/nl/" },
  { id: "pt", path: "/pt/" },
];
const dir = new URL(".", import.meta.url).pathname;
const out = `${dir}public`;
const escape = (s) => s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const template = readFileSync(`${dir}template.html`, "utf8");
const strings = Object.fromEntries(
  LOCALES.map((l) => [l.id, JSON.parse(readFileSync(`${dir}strings/${l.id}.json`, "utf8"))]),
);

const alternates = [...LOCALES, { id: "x-default", path: "/en/" }]
  .map((l) => `<link rel="alternate" hreflang="${l.id}" href="${SITE}${l.path}">`)
  .join("\n    ");

rmSync(out, { recursive: true, force: true });
cpSync(`${dir}static`, out, { recursive: true });

for (const locale of LOCALES) {
  const s = strings[locale.id];
  const languages = LOCALES.map((l) => {
    const current = l.id === locale.id ? ' aria-current="page"' : "";
    return `<a href="${l.path}" hreflang="${l.id}" lang="${l.id}"${current}>${escape(strings[l.id].languageName)}</a></li>`;
  }).join("\n        ");
  const html = template
    .replaceAll("{{alternates}}", alternates)
    .replaceAll("{{languages}}", languages)
    .replaceAll("{{canonical}}", `${SITE}${locale.path}`)
    .replaceAll("{{homePath}}", locale.path)
    .replace(/\{\{(\w+)\}\}/g, (_, key) => {
      if (!(key in s)) throw new Error(`strings/${locale.id}.json is missing "${key}"`);
      return escape(s[key]);
    });
  mkdirSync(`${out}${locale.path}`, { recursive: true });
  writeFileSync(`${out}${locale.path}index.html`, html);
}
console.log(`built ${LOCALES.length} pages into public/`);
