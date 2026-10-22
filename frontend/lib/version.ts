export type AppVersion = { version: string; revision: string };

/** What the dev-only version line shows (ZIF-158), or null unless ZIF_SHOW_VERSION is exactly "true".
 * Read per request on the server, never inlined at build time: the value differs per deployment. */
export function appVersion(env: Record<string, string | undefined> = process.env): AppVersion | null {
  if (env.ZIF_SHOW_VERSION !== "true") return null;
  return {
    version: (env.ZIF_APP_VERSION ?? "dev").replace(/^v/, ""),
    revision: (env.ZIF_APP_REVISION ?? "dev").slice(0, 7),
  };
}

/** The version as the line shows it: marked -dev, since only non-production deployments show it.
 * version.json and /api/healthz keep the plain version, which matches the release tag. */
export function versionLabel({ version }: AppVersion): string {
  return version === "dev" ? "dev" : `v${version}-dev`;
}
