// Manual OpenTelemetry spans for the web server's proxy, mirroring backend/app/tracing.py's rules
// (see docs/specs/zif-87-traces.md, R4/R5): a private, never-registered provider, so Next's own
// built-in spans stay no-ops, and every span here is a root -- the browser never chooses a trace.
import {
  type Span,
  SpanKind,
  SpanStatusCode,
  ROOT_CONTEXT,
  isSpanContextValid,
} from "@opentelemetry/api";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { detectResources, envDetector } from "@opentelemetry/resources";
import { BasicTracerProvider, BatchSpanProcessor, type SpanProcessor } from "@opentelemetry/sdk-trace-base";

let built: BasicTracerProvider | undefined;

// Lazy and memoized: built once, on first use, never registered globally (register() would make
// Next's own instrumentation start recording raw request URLs and paths).
export function provider(processors: SpanProcessor[] = []): BasicTracerProvider {
  if (built) return built;
  process.env.OTEL_SERVICE_NAME ??= "ziftbook-web";
  const spanProcessors: SpanProcessor[] = [...processors];
  // Same gate as the backend: without an endpoint the exporter would fail to connect on every
  // request.
  const endpoint = process.env.OTEL_EXPORTER_OTLP_ENDPOINT || process.env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT;
  if (endpoint) spanProcessors.push(new BatchSpanProcessor(new OTLPTraceExporter()));
  built = new BasicTracerProvider({
    resource: detectResources({ detectors: [envDetector] }),
    spanProcessors,
  });
  return built;
}

// Always a root: ROOT_CONTEXT, never whatever context is current, so an incoming traceparent
// (already deleted by proxy.ts per R5) could never be inherited even by accident.
export function startProxySpan(method: string): Span {
  return provider()
    .getTracer("ziftbook-web")
    .startSpan(
      `${method} /api`,
      { kind: SpanKind.SERVER, attributes: { "http.request.method": method, "http.route": "/api/:path*" } },
      ROOT_CONTEXT,
    );
}

export function traceparent(span: Span): string | undefined {
  const ctx = span.spanContext();
  if (!isSpanContextValid(ctx)) return undefined;
  // The web writes only 00/01 (the W3C sampled flag); the Python SDK writes 02/03 (the level-2
  // random bit), and both sides accept either.
  return `00-${ctx.traceId}-${ctx.spanId}-0${ctx.traceFlags & 1}`;
}

export function endProxySpan(span: Span, status: number, err?: unknown): void {
  span.setAttribute("http.response.status_code", status);
  if (status >= 500 || err !== undefined) {
    span.setStatus({ code: SpanStatusCode.ERROR });
    span.setAttribute("error.type", err instanceof Error ? err.constructor.name : String(status));
  }
  span.end();
}
