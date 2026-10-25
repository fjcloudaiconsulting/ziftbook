import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  // This will be an international product: no hard-coded message ever lands in a JSX tree.
  // `eslint-plugin-react` is already pulled in by eslint-config-next (nextVitals registers it),
  // so this needs no new dependency. allowedStrings is only true non-text: the wordmark, and the
  // punctuation a few screens join translated pieces with.
  {
    rules: {
      "react/jsx-no-literals": ["error", { allowedStrings: ["ziftbook", "·", "…", "–"] }],
      // ZIF-146: no string ever becomes markup. The booking and account pages share an origin with the
      // API that answers a signed-in person's details; merchant text renders only as text.
      "react/no-danger": "error",
      "no-restricted-syntax": [
        "error",
        {
          selector: "AssignmentExpression > MemberExpression.left[property.name=/^(innerHTML|outerHTML)$/]",
          message: "Never assign markup: render text with React (ZIF-146).",
        },
        {
          selector: "CallExpression[callee.property.name=/^(insertAdjacentHTML|createContextualFragment)$/]",
          message: "Never parse a string as markup (ZIF-146).",
        },
        {
          selector: "CallExpression[callee.object.name='document'][callee.property.name=/^(write|writeln)$/]",
          message: "Never parse a string as markup (ZIF-146).",
        },
      ],
    },
  },
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "api-client/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
  ]),
]);

export default eslintConfig;
