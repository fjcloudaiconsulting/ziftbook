import { defineConfig } from "@hey-api/openapi-ts";

// Generated from the committed API contract; the output is gitignored and rebuilt before dev, build and typecheck.
export default defineConfig({
  input: "../api/openapi.json",
  output: "api-client",
});
