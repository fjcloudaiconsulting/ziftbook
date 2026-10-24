import createMiddleware from "next-intl/middleware";
import { NextRequest, NextResponse } from "next/server";

import { routing } from "./i18n/routing";
import { CSP_HEADER, cspFor, newNonce, originOf } from "./lib/csp";
import { clientRequestId, errorFields, logger, requestId } from "./lib/log";
import { readNeedsSetup, setupGate, setupRedirect } from "./lib/setup-gate";
import { endProxySpan, startProxySpan, traceparent } from "./lib/trace";
import { apiUrl, clientIp, MAX_BODY_BYTES, WEBHOOK_MAX_BODY_BYTES } from "./lib/upstream";

const localize = createMiddleware(routing);
const log = logger("web.proxy");
const needsSetup = setupGate(readNeedsSetup);

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

function unavailable(id: string): NextResponse {
  return NextResponse.json(
    { code: "api_unavailable" },
    { status: 502, headers: { "X-Request-ID": id, "X-Content-Type-Options": "nosniff" } },
  );
}

// The whole request body, or null once it passes `limit` (ZIF-136). Read here, not streamed
// to the API: Next ends a body it cut at proxyClientMaxBodySize as if it were complete, and only
// counting the bytes tells the two apart. A client hanging up mid-upload aborts request.signal,
// which cancels the read (Next's copy of the body never ends on its own then); the fetch below
// then never starts, as the same signal is already aborted.
async function readBody(request: NextRequest, limit: number): Promise<Uint8Array<ArrayBuffer> | null> {
  const reader = request.body?.getReader();
  if (!reader) return new Uint8Array();
  const cancel = () => void reader.cancel();
  request.signal.addEventListener("abort", cancel, { once: true });
  try {
    // An abort before the listener was added never fires it.
    request.signal.throwIfAborted();
    const chunks: Uint8Array[] = [];
    let size = 0;
    for (;;) {
      const { done, value } = await reader.read();
      // Copied into a plain ArrayBuffer: fetch's BodyInit type refuses Buffer's ArrayBufferLike.
      if (done) return new Uint8Array(Buffer.concat(chunks));
      size += value.byteLength;
      if (size > limit) return null;
      chunks.push(value);
    }
  } finally {
    request.signal.removeEventListener("abort", cancel);
  }
}

// R1: fetch, not NextResponse.rewrite. A rewrite that can't connect makes Next answer 500
// text/plain itself, printing the upstream URL (with its query string) to stderr, and copies
// x-middleware-rewrite (the internal upstream URL) into the response. Owning the forward is the
// only way to a clean 502 and to keep secrets out of both logs and the response.
async function forwardApi(request: NextRequest): Promise<Response> {
  const id = requestId();
  const clientId = clientRequestId(request.headers.get("x-request-id"));
  const span = startProxySpan(request.method);
  const spanIds = {
    ...(clientId ? { client_request_id: clientId } : {}),
    trace_id: span.spanContext().traceId,
    span_id: span.spanContext().spanId,
  };
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
  const ip = clientIp(request.headers);
  if (ip) headers.set("x-forwarded-for", ip);
  headers.set("x-request-id", id);
  // A client's own id (ZB-29): never the request id, only passed along for correlation.
  headers.delete("x-client-request-id");
  if (clientId) headers.set("x-client-request-id", clientId);
  const tp = traceparent(span);
  if (tp) headers.set("traceparent", tp);

  try {
    const limit = pathname === "/api/stripe/webhook" ? WEBHOOK_MAX_BODY_BYTES : MAX_BODY_BYTES;
    const body = request.method === "GET" || request.method === "HEAD" ? undefined : await readBody(request, limit);
    if (body === null) {
      log.info("body too large", { request_id: id, method: request.method, ...spanIds });
      endProxySpan(span, 413);
      return NextResponse.json(
        { code: "content_too_large" },
        { status: 413, headers: { "X-Request-ID": id, "X-Content-Type-Options": "nosniff" } },
      );
    }
    const upstreamResponse = await fetch(new URL(pathname + search, upstream), {
      method: request.method,
      headers,
      body,
      redirect: "manual",
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(30_000)]),
    });
    // Headers iteration (unlike a naive Map) already yields each Set-Cookie as its own [name,
    // value] pair (measured), so no separate getSetCookie() rebuild is needed to keep them apart.
    // content-encoding/content-length are dropped because undici has already decoded the body;
    // the hop-by-hop set is dropped for the same reason it never reaches the upstream request.
    // server and x-middleware-* are the upstream's (or the framework's) own, never the client's.
    const responseHeaders = new Headers();
    for (const [name, value] of upstreamResponse.headers) {
      if (name === "content-encoding" || name === "content-length" || HOP_BY_HOP.includes(name)) continue;
      if (name === "server" || name.startsWith("x-middleware-")) continue;
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
      return new Response(null, { status: 499, headers: { "X-Content-Type-Options": "nosniff" } });
    }
    log.error("api unavailable", { request_id: id, method: request.method, ...spanIds, ...errorFields(err) });
    endProxySpan(span, 502, err);
    return unavailable(id);
  }
}

export default async function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;

  if (pathname === "/api" || pathname.startsWith("/api/")) {
    return forwardApi(request);
  }

  const setup = setupRedirect(pathname, routing.locales);
  if (setup && (await needsSetup())) return NextResponse.redirect(new URL(setup, request.url));

  // ZB-12: a fresh nonce for every page. Next reads it from the policy on the request and puts it on its
  // own scripts; x-nonce hands it to next/script (app/[locale]/layout.tsx). Whatever the client sent
  // under these names is dropped, so Next can never take its nonce from the client.
  const nonce = newNonce();
  // Read per request, like ZIF_API_URL: one build serves every deployment. The upload origin is the
  // one the API presigns photo uploads for (backend/app/storage.py: the public endpoint, else the endpoint).
  const policy = cspFor(
    nonce,
    originOf(process.env.ZIF_MEDIA_URL),
    originOf(process.env.ZIF_S3_PUBLIC_ENDPOINT || process.env.ZIF_S3_ENDPOINT),
  );
  const headers = new Headers(request.headers);
  for (const name of ["x-nonce", "content-security-policy", "content-security-policy-report-only"]) headers.delete(name);
  headers.set("x-nonce", nonce);
  headers.set(CSP_HEADER, policy);
  const response = localize(new NextRequest(request, { headers }));
  // Enforced, this replaces next.config.ts's frame-ancestors-only header (Next sends the proxy's value);
  // report-only, that header stays the one enforced policy, as a report-only policy enforces nothing.
  response.headers.set(CSP_HEADER, policy);
  return response;
}

export const config = {
  // /api is forwarded to FastAPI; every other page path goes through locale routing.
  // Next internals and files with an extension are skipped.
  // Next requires a static string literal here (it parses `config` at build time), so this can't
  // import LOCALE_MATCHER: it must stay byte-for-byte equal to it (tests/proxy-matcher.test.mjs
  // asserts the two never drift apart).
  matcher: ["/api/:path*", "/((?!api(?:/|$)|_next|_vercel|.*\\..*).*)"],
};
