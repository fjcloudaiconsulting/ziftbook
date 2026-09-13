import createMiddleware from "next-intl/middleware";
import { type NextRequest, NextResponse } from "next/server";

import { routing } from "./i18n/routing";

const localize = createMiddleware(routing);

// Read per request, never at build time: one image runs against any API.
function apiUrl(): string | undefined {
  return process.env.API_URL ?? (process.env.NODE_ENV === "development" ? "http://127.0.0.1:8000" : undefined);
}

export default function proxy(request: NextRequest) {
  const { pathname, search } = request.nextUrl;

  if (pathname === "/api" || pathname.startsWith("/api/")) {
    const upstream = apiUrl();
    if (!upstream) {
      console.error("API_URL is not set; cannot forward", pathname);
      return NextResponse.json({ code: "api_unavailable" }, { status: 502 });
    }
    return NextResponse.rewrite(new URL(`${pathname}${search}`, upstream));
  }

  return localize(request);
}

export const config = {
  // /api is forwarded to FastAPI; every other page path goes through locale routing.
  // Next internals and files with an extension are skipped.
  matcher: ["/api/:path*", "/((?!api|_next|_vercel|.*\\..*).*)"],
};
