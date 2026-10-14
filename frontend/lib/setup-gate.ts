// First-run gate (ZIF-70): while the API says no admin exists yet, every page sends the visitor to
// /setup. Pure and injectable so it is testable without next/server; proxy.ts wires in fetch.
import { apiUrl } from "./upstream.ts";

// GET /api/setup. Throws when the API can't be reached or answers anything but 200.
export async function readNeedsSetup(): Promise<boolean> {
  const upstream = apiUrl();
  if (!upstream) throw new Error("api url unset");
  const response = await fetch(new URL("/api/setup", upstream), { cache: "no-store", signal: AbortSignal.timeout(2_000) });
  if (!response.ok) throw new Error(`setup state ${response.status}`);
  const body = await response.json();
  // A 200 that isn't {needs_setup: boolean} is a broken upstream, never an answer ("false" gets cached).
  if (typeof body?.needs_setup !== "boolean") throw new Error("setup state malformed");
  return body.needs_setup;
}

// Once false is seen it is cached for the process lifetime (users never empties). A failed read
// neither redirects nor caches: the backend stays authoritative.
export function setupGate(read: () => Promise<boolean>): () => Promise<boolean> {
  let done = false;
  let pending: Promise<boolean> | undefined;
  // Concurrent requests share one read. While the API hangs, each page waits up to its 2s timeout.
  return async () => {
    if (done) return false;
    try {
      pending ??= read().finally(() => (pending = undefined));
      const needed = await pending;
      if (!needed) done = true;
      return needed;
    } catch {
      return false;
    }
  };
}

// Where to send a page request while setup is needed, or null for the setup page itself and /api.
export function setupRedirect(pathname: string, locales: readonly string[]): string | null {
  if (pathname === "/api" || pathname.startsWith("/api/")) return null;
  const locale = locales.find((l) => pathname === `/${l}` || pathname.startsWith(`/${l}/`));
  const target = `${locale ? `/${locale}` : ""}/setup`;
  return pathname === target || pathname === `${target}/` ? null : target;
}
