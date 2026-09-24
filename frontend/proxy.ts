import { isIP } from "node:net";

import createMiddleware from "next-intl/middleware";
import { type NextRequest, NextResponse } from "next/server";

import { routing } from "./i18n/routing";
import { errorFields, logger, requestId } from "./lib/log";

const localize = createMiddleware(routing);
const log = logger("web.proxy");

// Headers that name the visitor. Only the one this deployment trusts is forwarded, as
// X-Forwarded-For.
const FORWARDING = ["x-forwarded-for", "x-real-ip", "forwarded", "x-forwarded-proto"];
// Never forwarded upstream: hop-by-hop headers (RFC 7230 6.1), the incoming Host, and
// content-length (undici sets its own once the body and headers are final). expect is here too:
// undici throws UND_ERR_NOT_SUPPORTED on it, and curl sends it for any upload over 1MB.
const STRIP = ["host", "connection", "keep-alive", "transfer-encoding", "te", "upgrade", "proxy-connection", "expect", "content-length"];

// Read per request, like apiUrl(). Set only where every request reaches this server through a
// proxy that overwrites the header (staging: cf-connecting-ip). Unset: the API sees this
// server's address.
function clientIp(request: NextRequest): string | undefined {
  const name = process.env.ZIF_CLIENT_IP_HEADER;
  const value = name ? (request.headers.get(name) ?? "").trim().replace(/^::ffff:(?=\d+\.)/i, "") : "";
  return isIP(value) ? value : undefined;
}

// Read per request, never at build time: one image runs against any API.
function apiUrl(): string | undefined {
  return process.env.ZIF_API_URL ?? (process.env.NODE_ENV === "development" ? "http://127.0.0.1:8000" : undefined);
}

function unavailable(id: string): NextResponse {
  return NextResponse.json({ code: "api_unavailable" }, { status: 502, headers: { "X-Request-ID": id } });
}

// R1: fetch, not NextResponse.rewrite. A rewrite that can't connect makes Next answer 500
// text/plain itself, printing the upstream URL (with its query string) to stderr, and copies
// x-middleware-rewrite (the internal upstream URL) into the response. Owning the forward is the
// only way to a clean 502 and to keep secrets out of both logs and the response.
async function forwardApi(request: NextRequest): Promise<Response> {
  const id = requestId(request.headers.get("x-request-id"));
  const upstream = apiUrl();
  if (!upstream) {
    log.error("api url unset", { request_id: id, method: request.method });
    return unavailable(id);
  }

  const { pathname, search } = request.nextUrl;
  const headers = new Headers(request.headers);
  for (const name of [...FORWARDING, ...STRIP]) headers.delete(name);
  const ip = clientIp(request);
  if (ip) headers.set("x-forwarded-for", ip);
  headers.set("x-request-id", id);

  try {
    const upstreamResponse = await fetch(
      new URL(pathname + search, upstream),
      {
        method: request.method,
        headers,
        body: request.method === "GET" || request.method === "HEAD" ? undefined : request.body,
        duplex: "half",
        redirect: "manual",
        signal: AbortSignal.any([request.signal, AbortSignal.timeout(30_000)]),
        // duplex isn't in lib.dom.d.ts's RequestInit yet, though Node's fetch (undici) requires it
        // for a streamed body.
      } as RequestInit & { duplex: "half" },
    );
    // Rebuilt, never passed through: plain iteration folds every Set-Cookie into one
    // comma-joined header, which breaks a cookie whose Expires contains a comma.
    const responseHeaders = new Headers();
    for (const [name, value] of upstreamResponse.headers) {
      if (name === "content-encoding" || name === "content-length" || name === "set-cookie") continue;
      responseHeaders.append(name, value);
    }
    for (const cookie of upstreamResponse.headers.getSetCookie()) responseHeaders.append("set-cookie", cookie);
    return new Response(upstreamResponse.body, { status: upstreamResponse.status, headers: responseHeaders });
  } catch (err) {
    if (request.signal.aborted) {
      log.debug("client closed", { request_id: id, method: request.method });
      return new Response(null, { status: 499 });
    }
    log.error("api unavailable", { request_id: id, method: request.method, ...errorFields(err) });
    return unavailable(id);
  }
}

export default function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;

  if (pathname === "/api" || pathname.startsWith("/api/")) {
    return forwardApi(request);
  }

  return localize(request);
}

export const config = {
  // /api is forwarded to FastAPI; every other page path goes through locale routing.
  // Next internals and files with an extension are skipped. proxy.ts (renamed from
  // middleware.ts) always runs on the Node.js runtime, so node:crypto, process.stdout and a
  // duplex-streamed fetch all just work.
  matcher: ["/api/:path*", "/((?!api|_next|_vercel|.*\\..*).*)"],
};
