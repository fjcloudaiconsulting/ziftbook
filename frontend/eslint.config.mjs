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
      "react/jsx-no-literals": ["error", { allowedStrings: ["ziftbook", "·", "…"] }],
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
