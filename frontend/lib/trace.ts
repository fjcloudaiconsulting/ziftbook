// Manual OpenTelemetry spans for the web server's proxy, mirroring backend/app/tracing.py's rules
// (see docs/specs/zif-87-traces.md, R4/R5): a private, never-registered provider, so Next's own
// built-in spans stay no-ops, and every span here is a root -- the browser never chooses a trace.
import {
  type Attributes,
  type Span,
  SpanKind,
  SpanStatusCode,
  ROOT_CONTEXT,
  isSpanContextValid,
} from "@opentelemetry/api";
import { OTLPMetricExporter } from "@opentelemetry/exporter-metrics-otlp-http";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { detectResources, envDetector } from "@opentelemetry/resources";
import { MeterProvider, type MetricReader, PeriodicExportingMetricReader } from "@opentelemetry/sdk-metrics";
import { BasicTracerProvider, BatchSpanProcessor, type SpanProcessor } from "@opentelemetry/sdk-trace-base";

let built: BasicTracerProvider | undefined;
let builtMeter: MeterProvider | undefined;
// performance.now() and method at span start: the span may be non-recording (always_off), so its
// own startTime and attributes are not usable.
const started = new WeakMap<Span, { at: number; method: string }>();
const STANDARD_METHODS = new Set(["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "CONNECT", "TRACE"]);

// The gate: an endpoint (the signal-specific or generic
// OTEL_EXPORTER_OTLP_*_ENDPOINT, trimmed) and OTEL_<signal>_EXPORTER, trimmed and lower-cased, not
// "none". `||`, not `??`, for the signal-specific-to-generic endpoint fallback: an empty
// OTEL_EXPORTER_OTLP_TRACES_ENDPOINT must still fall through to the generic one.
export function enabled(signal: "TRACES" | "METRICS" = "TRACES"): boolean {
  const endpoint = (process.env[`OTEL_EXPORTER_OTLP_${signal}_ENDPOINT`] || process.env.OTEL_EXPORTER_OTLP_ENDPOINT || "").trim();
  if (!endpoint) return false;
  const exporter = (process.env[`OTEL_${signal}_EXPORTER`] ?? "").trim().toLowerCase();
  return exporter !== "none";
}

// Lazy and memoized: built once, on first use, never registered globally (register() would make
// Next's own instrumentation start recording raw request URLs and paths).
export function provider(processors: SpanProcessor[] = []): BasicTracerProvider {
  if (built) return built;
  process.env.OTEL_SERVICE_NAME ??= "ziftbook-web";
  const spanProcessors: SpanProcessor[] = [...processors];
  if (enabled()) spanProcessors.push(new BatchSpanProcessor(new OTLPTraceExporter()));
  built = new BasicTracerProvider({
    resource: detectResources({ detectors: [envDetector] }),
    spanProcessors,
  });
  return built;
}

// Same rules as provider(): lazy, memoized, never registered globally (no setGlobalMeterProvider).
export function meterProvider(readers: MetricReader[] = []): MeterProvider {
  if (builtMeter) return builtMeter;
  process.env.OTEL_SERVICE_NAME ??= "ziftbook-web";
  const metricReaders = [...readers];
  if (enabled("METRICS")) metricReaders.push(new PeriodicExportingMetricReader({ exporter: new OTLPMetricExporter() }));
  builtMeter = new MeterProvider({ resource: detectResources({ detectors: [envDetector] }), readers: metricReaders });
  return builtMeter;
}

// Always a root: ROOT_CONTEXT, never whatever context is current, so an incoming traceparent
// (already deleted by proxy.ts per R5) could never be inherited even by accident.
export function startProxySpan(method: string): Span {
  const span = provider()
    .getTracer("ziftbook-web")
    .startSpan(
      `${method} /api`,
      { kind: SpanKind.SERVER, attributes: { "http.request.method": method, "http.route": "/api/:path*" } },
      ROOT_CONTEXT,
    );
  started.set(span, { at: performance.now(), method });
  return span;
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
  let errorType: string | undefined;
  if (status >= 500 || err !== undefined) {
    span.setStatus({ code: SpanStatusCode.ERROR });
    errorType = err instanceof Error ? err.constructor.name : String(status);
    span.setAttribute("error.type", errorType);
  }
  span.end();
  const { at, method } = started.get(span) ?? { at: performance.now(), method: "" };
  const attributes: Attributes = {
    "http.request.method": STANDARD_METHODS.has(method) ? method : "_OTHER",
    "http.route": "/api/:path*",
    "http.response.status_code": status,
  };
  if (errorType !== undefined) attributes["error.type"] = errorType;
  meterProvider()
    .getMeter("ziftbook-web")
    .createHistogram("http.server.request.duration", {
      unit: "s",
      advice: { explicitBucketBoundaries: [0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1, 2.5, 5, 7.5, 10] },
    })
    .record((performance.now() - at) / 1000, attributes);
}
