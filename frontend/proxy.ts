import { isIP } from "node:net";

import createMiddleware from "next-intl/middleware";
import { type NextRequest, NextResponse } from "next/server";

import { routing } from "./i18n/routing";
import { errorFields, logger, requestId } from "./lib/log";
import { endProxySpan, startProxySpan, traceparent } from "./lib/trace";

const localize = createMiddleware(routing);
const log = logger("web.proxy");

// Headers that name the visitor. Only the one this deployment trusts is forwarded, as
// X-Forwarded-For.
const FORWARDING = ["x-forwarded-for", "x-real-ip", "forwarded", "x-forwarded-proto"];
// Hop-by-hop headers (RFC 7230 6.1): meaningful only between one connection's two ends, never
// valid on a proxied message, request or response.
const HOP_BY_HOP = ["connection", "keep-alive", "transfer-encoding", "te", "upgrade", "proxy-connection", "trailer"];
// Stripped from the request only: the incoming Host, content-length (undici sets its own once the
// body and headers are final), expect (undici throws UND_ERR_NOT_SUPPORTED on it, and curl sends
// it for any upload over 1MB), and proxy-authorization (meant for a proxy between the browser and
// us, never for the upstream).
const REQUEST_ONLY_STRIP = ["host", "expect", "content-length", "proxy-authorization"];
// R5: clients never choose a trace. Dropped unconditionally, from the request only: the API
// trusts the traceparent this server sets itself below.
const TRACE_HEADERS = ["traceparent", "tracestate", "baggage"];

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
  const span = startProxySpan(request.method);
  const spanIds = { trace_id: span.spanContext().traceId, span_id: span.spanContext().spanId };
  const upstream = apiUrl();
  if (!upstream) {
    log.error("api url unset", { request_id: id, method: request.method, ...spanIds });
    endProxySpan(span, 502);
    return unavailable(id);
  }

  const { pathname, search } = request.nextUrl;
  const headers = new Headers(request.headers);
  // RFC 7230 6.1 also wants every header the Connection value itself names dropped. Not parsed:
  // the upstream is our own backend, which never reads an extra hop header even if one arrives,
  // and Connection's value is arbitrary client input, some of which isn't a valid header name and
  // makes Headers.delete() throw (an invalid Connection value must never 500 the proxy).
  for (const name of [...FORWARDING, ...HOP_BY_HOP, ...REQUEST_ONLY_STRIP, ...TRACE_HEADERS]) headers.delete(name);
  const ip = clientIp(request);
  if (ip) headers.set("x-forwarded-for", ip);
  headers.set("x-request-id", id);
  const tp = traceparent(span);
  if (tp) headers.set("traceparent", tp);

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
    // Headers iteration (unlike a naive Map) already yields each Set-Cookie as its own [name,
    // value] pair (measured), so no separate getSetCookie() rebuild is needed to keep them apart.
    // content-encoding/content-length are dropped because undici has already decoded the body;
    // the hop-by-hop set is dropped for the same reason it never reaches the upstream request.
    const responseHeaders = new Headers();
    for (const [name, value] of upstreamResponse.headers) {
      if (name === "content-encoding" || name === "content-length" || HOP_BY_HOP.includes(name)) continue;
      responseHeaders.append(name, value);
    }
    // Built before ending the span: a throw from the Response constructor must not end it twice.
    const proxied = new Response(upstreamResponse.body, {
      status: upstreamResponse.status,
      headers: responseHeaders,
    });
    endProxySpan(span, upstreamResponse.status);
    return proxied;
  } catch (err) {
    if (request.signal.aborted) {
      log.debug("client closed", { request_id: id, method: request.method, ...spanIds });
      endProxySpan(span, 499);
      return new Response(null, { status: 499 });
    }
    log.error("api unavailable", { request_id: id, method: request.method, ...spanIds, ...errorFields(err) });
    endProxySpan(span, 502, err);
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
  // Next internals and files with an extension are skipped.
  matcher: ["/api/:path*", "/((?!api|_next|_vercel|.*\\..*).*)"],
};
