import { isIP } from "node:net";

import createMiddleware from "next-intl/middleware";
import { type NextRequest, NextResponse } from "next/server";

import { routing } from "./i18n/routing";

const localize = createMiddleware(routing);

// Headers that name the visitor. Only the one this deployment trusts is forwarded, as X-Forwarded-For.
const FORWARDING = ["x-forwarded-for", "x-real-ip", "forwarded", "x-forwarded-proto"];

// Read per request, like apiUrl(). Set only where every request reaches this server through a proxy
// that overwrites the header (staging: cf-connecting-ip). Unset: the API sees this server's address.
function clientIp(request: NextRequest): string | undefined {
  const name = process.env.ZIF_CLIENT_IP_HEADER;
  const value = name ? (request.headers.get(name) ?? "").trim().replace(/^::ffff:(?=\d+\.)/i, "") : "";
  return isIP(value) ? value : undefined;
}

// Read per request, never at build time: one image runs against any API.
function apiUrl(): string | undefined {
  return process.env.ZIF_API_URL ?? (process.env.NODE_ENV === "development" ? "http://127.0.0.1:8000" : undefined);
}

export default function proxy(request: NextRequest) {
  const { pathname, search } = request.nextUrl;

  if (pathname === "/api" || pathname.startsWith("/api/")) {
    const upstream = apiUrl();
    if (!upstream) {
      console.error("ZIF_API_URL is not set; cannot forward", pathname);
      return NextResponse.json({ code: "api_unavailable" }, { status: 502 });
    }
    const headers = new Headers(request.headers);
    for (const name of FORWARDING) headers.delete(name);
    const ip = clientIp(request);
    if (ip) headers.set("x-forwarded-for", ip);
    return NextResponse.rewrite(new URL(`${pathname}${search}`, upstream), { request: { headers } });
  }

  return localize(request);
}

export const config = {
  // /api is forwarded to FastAPI; every other page path goes through locale routing.
  // Next internals and files with an extension are skipped.
  matcher: ["/api/:path*", "/((?!api|_next|_vercel|.*\\..*).*)"],
};
