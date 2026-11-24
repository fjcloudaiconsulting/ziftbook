import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { checkCodeStrings, checkTree } from "../scripts/check-code-strings.mjs";

const fails = (source) => assert.ok(checkCodeStrings(source).length > 0, source);
const passes = (source) => assert.deepEqual(checkCodeStrings(source), [], source);

test("catches the original week helper, literals, concatenation and sentence fragments", () => {
  assert.deepEqual(checkCodeStrings('export const range = (a: string, b: string) => `${a} to ${b}`;', "lib/week.ts"), [
    { line: 1, column: 48, text: "${…} to ${…}" },
  ]);
  for (const source of [
    'const result = "Please save your changes";',
    'const result = "Welcome";',
    'const result = a + " to " + b;',
    'const result = a + "to" + b;',
    'const result = `Ready. Your booking is ${state}.`;',
    'const result = `Save changes`;',
    'const result = `${count} bookings`;',
  ]) fails(source);
});

test("catches lowercase and uppercase words in common UI text positions", () => {
  for (const source of [
    'const label = "save";',
    'const config = {label: "save"};',
    'const config = {title: "SAVE"};',
    'const config = {description: "Try-again"};',
    'const ui = <button>{"save"}</button>;',
    'const ui = <input placeholder="save" aria-label="SAVE" />;',
    'const ui = <img alt={"photo"} />;',
  ]) fails(source);
});

test("keys, comparison operands and type literals are machine data, prose elsewhere is not", () => {
  passes('const state = "pending"; const key = "booking.confirm"; t("Confirm booking");');
  passes('const item = {"Confirm booking": count}; const value = item["Confirm booking"];');
  passes('type State = "Confirm booking"; const x = status === "Confirm booking";');
  passes('switch (status) { case "Confirm booking": break; }');
  fails('const result = "Confirm booking";');
  fails('const result = transform("Confirm booking");');
  fails('t("key", {name: "Confirm booking"});');
});

test("CSS and element identifiers are allowed only in technical positions", () => {
  passes('const ui = <div className={`booking ${active ? "is-active" : ""}`} id="booking-heading" />;');
  passes('const ui = <div style={{fontFamily: "Some Font"}} aria-describedby="booking-hint booking-error" />;');
  passes('document.querySelector("button, input");');
  passes('const id = `${base}-heading`;');
  fails('const label = "booking heading";');
  fails('const ui = <div title="booking heading" />;');
  fails('const result = "Some Font";');
  fails('const result = "button, input";');
});

test("URLs pass while their words in prose still fail", () => {
  passes('const url = "https://example.test/book"; const path = `/api/${id}`;');
  passes('const address = "person@example.test";');
  fails('const result = "Visit https://example.test/book";');
  fails('const result = `${host} to ${path}`;');
});

test("console log text passes without suppressing prose in other calls or callbacks", () => {
  passes('console.warn(`Booking failed for ${id}`);');
  fails('const result = `Booking failed for ${id}`;');
  fails('showMessage("Booking failed");');
  fails('throw new Error("Booking failed");');
  fails('console.log(() => { return "Booking failed"; });');
});

test("reasoned allow comments apply only to their own or immediately following line", () => {
  passes('// i18n-allow: executable command syntax\nconst command = "run this command";');
  passes('const commands = ["run this command", // i18n-allow: executable command syntax\n];');
  passes('const value = `Command ${name}`; // i18n-allow: executable command syntax');
  passes('const input = <input placeholder={/* i18n-allow: recovery code format */ "XXXX-XXXX"} />;');
  fails('// i18n-allow:\nconst value = "Save changes";');
  fails('// i18n-allow: \nconst value = "Save changes";');
  fails('// i18n-allow: a distant comment\n\nconst value = "Save changes";');
  fails('const a = "Allowed command"; // i18n-allow: executable command syntax\nconst b = "Save changes";');
  assert.equal(checkCodeStrings('// i18n-allow: first element only\nconst values = ["Allowed command",\n"Save changes"];').length, 1);
  fails('const value = "// i18n-allow: forged comment";');
  fails('const value = `// i18n-allow: forged comment`;');
});

test("comments and regex literals are not text, but invalid syntax fails closed", () => {
  passes('// Save changes\nconst pattern = /Save changes/;');
  passes('"use client"; const spacer = `${a} · ${b}`;');
  fails('const label = "use client";');
  assert.throws(() => checkCodeStrings('const broken = "unterminated'), /invalid syntax/);
});

test("scans lib and component files and derives legal catalogs from message catalog filenames", () => {
  const scratch = fileURLToPath(new URL("../../.claude/tmp/", import.meta.url));
  mkdirSync(scratch, { recursive: true });
  const root = mkdtempSync(resolve(scratch, "code-strings-"));
  try {
    for (const dir of ["lib/legal", "app/deep", "messages"]) mkdirSync(resolve(root, dir), { recursive: true });
    writeFileSync(resolve(root, "messages/de.json"), '{"label":"Speichern"}');
    writeFileSync(resolve(root, "lib/legal/de.ts"), 'export const de = {title: "Legal terms"};');
    writeFileSync(resolve(root, "lib/legal/index.ts"), 'export const text = "Save changes";');
    writeFileSync(resolve(root, "lib/week.ts"), 'export const range = (a, b) => `${a} to ${b}`;');
    writeFileSync(resolve(root, "app/deep/component.tsx"), 'const ui = <input placeholder="Save changes" />;');
    writeFileSync(resolve(root, "app/deep/helper.js"), 'const label = "Save changes";');
    assert.deepEqual(checkTree(root).map(({file}) => file).sort(), ["app/deep/component.tsx", "app/deep/helper.js", "lib/legal/index.ts", "lib/week.ts"]);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("CLI checks the real frontend independently of the working directory", () => {
  const script = fileURLToPath(new URL("../scripts/check-code-strings.mjs", import.meta.url));
  const result = spawnSync(process.execPath, [script], { cwd: fileURLToPath(new URL("../../", import.meta.url)), encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /no uncatalogued text/);
});
