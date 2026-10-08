// Where the API lives, and which header names the real visitor: shared by proxy.ts (forwarding
// every /api/* request) and the booking page's server loader (forwarding its own GET so the API
// sees the same client the proxy would have forwarded for it).
import { isIP } from "node:net";

// Read per request, like apiUrl(). Set only where every request reaches this server through a
// proxy that overwrites the header (production and staging: cf-connecting-ip). Unset: the API
// sees this server's address.
export function clientIp(headers: { get(name: string): string | null }): string | undefined {
  const name = process.env.ZIF_CLIENT_IP_HEADER;
  const value = name ? (headers.get(name) ?? "").trim().replace(/^::ffff:(?=\d+\.)/i, "") : "";
  return isIP(value) ? value : undefined;
}

// Read per request, never at build time: one image runs against any API.
export function apiUrl(): string | undefined {
  return process.env.ZIF_API_URL ?? (process.env.NODE_ENV === "development" ? "http://127.0.0.1:8000" : undefined);
}

// proxy.ts's locale-routing matcher, factored out so it is testable without importing next/server
// (F7): every path except /api itself (not merely prefixed by it, so a public slug like "apiary"
// still gets locale routing), Next internals, and files with an extension.
export const LOCALE_MATCHER = "/((?!api(?:/|$)|_next|_vercel|.*\\..*).*)";

// The largest request body proxy.ts forwards: the API's own MAX_BODY (backend/app/main.py), kept
// equal by tests/proxy.test.mjs. Read here as well, because Next hands the proxy a body it cut at
// proxyClientMaxBodySize (next.config.ts) as if it were complete.
export const MAX_BODY_BYTES = 64 * 1024;
