// Code strings belong in catalogs unless their syntax or use identifies machine data.
import { readdirSync, readFileSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import { resolve } from "node:path";
import ts from "typescript";

const TECHNICAL_ATTRIBUTES = new Set("className id href src rel target type name htmlFor role autoComplete inputMode pattern method action key d viewBox fill stroke strokeWidth strokeLinecap strokeLinejoin transform clipPath width height xmlns lang dir style accept aria-describedby aria-labelledby aria-controls aria-owns".split(" "));

function machineText(text) {
  return /^(?:https?:\/\/|mailto:|tel:|\/|\.\.?\/|@\/|#)[^\s]*$/.test(text)
    || /^[a-z0-9][a-zA-Z0-9{}]*(?:[._:-][a-zA-Z0-9{}$?=&%+@#-]+)+$/.test(text.trim())
    || /^--[a-z0-9-]+$/.test(text)
    || /^(?:image|application|text|audio|video|font|multipart)\/[a-z0-9.+-]+(?:;[a-z0-9-]+=[a-z0-9-]+)*$/.test(text)
    || /^[A-Z0-9_]+$/.test(text)
    || /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(text)
    || /^(?:[0-9.]+|\{\})(?:px|rem|em|vh|vw|s|ms)$/.test(text);
}

function valueExpression(node) {
  let p = node.parent;
  while (p && (ts.isParenthesizedExpression(p) || ts.isAsExpression(p) || ts.isSatisfiesExpression(p)
    || (ts.isConditionalExpression(p) && node !== p.condition)
    || (ts.isBinaryExpression(p) && p.operatorToken.kind === ts.SyntaxKind.QuestionQuestionToken))) {
    node = p;
    p = node.parent;
  }
  return node;
}

function technical(node) {
  const value = valueExpression(node);
  for (let p = node.parent, child = node; p; child = p, p = p.parent) {
    if (ts.isTypeNode(p) || ts.isImportDeclaration(p) || ts.isExportDeclaration(p)) return true;
    if ((ts.isPropertyAssignment(p) || ts.isPropertySignature(p)) && child === p.name) return true;
    if (ts.isElementAccessExpression(p) && child === p.argumentExpression) return true;
    if (ts.isBinaryExpression(p) && [ts.SyntaxKind.EqualsEqualsEqualsToken, ts.SyntaxKind.ExclamationEqualsEqualsToken, ts.SyntaxKind.EqualsEqualsToken, ts.SyntaxKind.ExclamationEqualsToken].includes(p.operatorToken.kind)) return true;
    if (ts.isCaseClause(p) && child === p.expression) return true;
    if (ts.isJsxAttribute(p)) {
      if (!/^[a-z]/.test(p.parent.parent.tagName.getText())) return false;
      const initializer = ts.isJsxExpression(p.initializer) ? p.initializer.expression : p.initializer;
      const property = value.parent;
      const css = p.name.getText() === "style" && ts.isPropertyAssignment(property)
        && property.initializer === value && valueExpression(property.parent) === initializer && !visible(node)
        && property.name.getText() !== "content";
      return css || (initializer === value && (TECHNICAL_ATTRIBUTES.has(p.name.getText()) || p.name.getText().startsWith("data-")));
    }
    if (ts.isCallExpression(p)) {
      const name = p.expression.getText();
      if (/^console\.(?:log|info|warn|error|debug|trace)$/.test(name)) return true;
      if (/^(?:t|useTranslations|getTranslations)(?:\.(?:rich|raw|has|markup))?$/.test(name) && child === p.arguments[0]) return true;
      if (/\.(?:querySelector|querySelectorAll|closest|matches|getPropertyValue|setProperty)$/.test(name) && child === p.arguments[0]) return true;
    }
    if (ts.isNewExpression(p) && /^(?:RegExp|Date)$/.test(p.expression.getText())) return true;
    if (ts.isStatement(p) || ts.isFunctionLike(p)) break;
  }
  return false;
}

function visible(node) {
  for (let p = node.parent; p; p = p.parent) {
    if (ts.isCallExpression(p)) return false;
    if (ts.isJsxAttribute(p)) return /^(?:title|alt|placeholder|aria-label|aria-description)$/.test(p.name.getText());
    if (ts.isPropertyAssignment(p)) return /^(?:title|label|text|heading|description|placeholder|alt|message)$/.test(p.name.getText());
    if (ts.isJsxExpression(p) && !ts.isJsxAttribute(p.parent)) return true;
    if (ts.isVariableDeclaration(p)) return /^(?:title|label|text|heading|description|placeholder|message)$/.test(p.name.getText());
    if (ts.isStatement(p) || ts.isFunctionLike(p)) break;
  }
  return false;
}

function allowed(node, source) {
  const startLine = source.getLineAndCharacterOfPosition(node.getStart(source)).line;
  const endLine = source.getLineAndCharacterOfPosition(node.end).line;
  for (let p = node; p && !ts.isSourceFile(p); p = p.parent) {
    let after = p.end;
    while (/[;,)}\] \t]/.test(source.text[after] ?? "")) after++;
    const comments = [...(ts.getLeadingCommentRanges(source.text, p.pos) ?? []), ...(ts.getTrailingCommentRanges(source.text, p.pos) ?? []), ...(ts.getTrailingCommentRanges(source.text, after) ?? [])];
    if (comments.some(({ pos, end }) => {
      const token = ts.getTokenAtPosition(source, pos);
      if (token.getStart(source) <= pos && (ts.isJsxText(token) || ts.isStringLiteral(token) || ts.isTemplateLiteralToken(token))) return false;
      const line = source.getLineAndCharacterOfPosition(pos).line;
      const standalone = /^\s*$/.test(source.text.slice(source.getPositionOfLineAndCharacter(line, 0), pos));
      const reason = source.text.slice(pos + 2, end).replace(/\*\/$/, "").trim();
      return ((standalone && line === startLine - 1) || line === startLine || line === endLine)
        && /^i18n-allow:[ \t]*\S[^\r\n]*$/.test(reason);
    })) return true;
    if (ts.isStatement(p)) break;
  }
  return false;
}

export function checkCodeStrings(text, filename = "input.tsx") {
  const source = ts.createSourceFile(filename, text, ts.ScriptTarget.Latest, true);
  if (source.parseDiagnostics.length) throw new Error(`${filename}: cannot check invalid syntax`);
  const problems = [];
  function visit(node) {
    if (ts.isTypeNode(node) || ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) return;
    const pieces = ts.isTemplateExpression(node)
      ? [node.head.text, ...node.templateSpans.map((span) => span.literal.text)]
      : ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node) ? [node.text] : null;
    const value = pieces?.join("{}");
    const expression = pieces && valueExpression(node);
    const first = ts.isTemplateExpression(node) && node.templateSpans[0].expression;
    const id = value && /^\{\}[._:-][a-zA-Z0-9{}._:-]+$/.test(value) && !visible(node)
      && ((first && ts.isIdentifier(first) && /^(?:id|idBase|key)$|(?:Id|ID|Key)$/.test(first.text))
        || (ts.isVariableDeclaration(expression.parent) && /^(?:id|key)$|(?:Id|ID|Key)$/.test(expression.parent.name.getText())));
    const path = value && /^\{\}\/[^\s]*$/.test(value) && !visible(node);
    // shortcut: unlabelled identifier-shaped tokens can be keys or words; use data-flow analysis if that ambiguity becomes a source of leaks.
    const token = value && /^[a-z_][a-zA-Z0-9_]*$/.test(value) && !visible(node)
      && !(ts.isBinaryExpression(node.parent) && node.parent.operatorToken.kind === ts.SyntaxKind.PlusToken);
    const directive = pieces && ts.isExpressionStatement(node.parent) && ["use client", "use server", "use strict"].includes(value);
    if (pieces && pieces.some((piece) => /\p{L}/u.test(piece)) && !token && !directive && !id && !path && !(machineText(value) && !visible(node)) && !technical(node) && !allowed(node, source)) {
      const { line, character } = source.getLineAndCharacterOfPosition(node.getStart(source));
      problems.push({ line: line + 1, column: character + 1, text: pieces.join("${…}") });
    }
    ts.forEachChild(node, visit);
  }
  visit(source);
  return problems;
}

export function checkTree(root) {
  const problems = [];
  const catalogs = new Set(readdirSync(resolve(root, "messages")).filter((name) => name.endsWith(".json")).map((name) => `lib/legal/${name.slice(0, -5)}.ts`));
  function scan(dir) {
    for (const entry of readdirSync(resolve(root, dir), { withFileTypes: true })) {
      const file = `${dir}/${entry.name}`;
      if (entry.isDirectory()) scan(file);
      else if (/\.[cm]?[jt]sx?$/.test(file)) {
        // These are already translated legal catalogs, not executable UI helpers.
        if (catalogs.has(file)) continue;
        for (const problem of checkCodeStrings(readFileSync(resolve(root, file), "utf8"), file)) problems.push({ file, ...problem });
      }
    }
  }
  scan("lib");
  scan("app");
  return problems;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  const problems = checkTree(fileURLToPath(new URL("../", import.meta.url)));
  for (const { file, line, column, text } of problems) console.error(`${file}:${line}:${column}: move ${JSON.stringify(text)} to a message catalog, or add // i18n-allow: <reason>`);
  if (problems.length) process.exitCode = 1;
  else console.log("code strings: no uncatalogued text");
}
