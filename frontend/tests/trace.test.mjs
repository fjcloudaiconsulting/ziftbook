// lib/trace.ts: the proxy's manual spans. W1 (table id) in docs/specs/zif-87-traces.md.
// provider() memoizes a single BasicTracerProvider per module instance, so each scenario that
// needs its own env/processors re-imports the module with a cache-busting query string.
import assert from "node:assert/strict";
import { describe, test } from "node:test";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";

const MODULE_PATH = "../lib/trace.ts";

function freshTrace() {
  return import(`${MODULE_PATH}?t=${Math.random()}`);
}

describe("lib/trace.ts (W1)", () => {
  test("a proxy span's attribute keys are exactly the allowlist; traceparent carries its ids", async () => {
    delete process.env.OTEL_EXPORTER_OTLP_ENDPOINT;
    delete process.env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT;
    const { provider, startProxySpan, traceparent, endProxySpan } = await freshTrace();
    provider();
    const span = startProxySpan("GET");
    assert.deepEqual(Object.keys(span.attributes).sort(), ["http.request.method", "http.route"]);
    assert.equal(span.attributes["http.request.method"], "GET");
    assert.equal(span.attributes["http.route"], "/api/:path*");

    const ctx = span.spanContext();
    const tp = traceparent(span);
    assert.match(tp, /^00-[0-9a-f]{32}-[0-9a-f]{16}-0[01]$/);
    assert.equal(tp, `00-${ctx.traceId}-${ctx.spanId}-0${ctx.traceFlags & 1}`);
    endProxySpan(span, 200);
  });

  test("with no endpoint, only the processors passed in are attached", async () => {
    delete process.env.OTEL_EXPORTER_OTLP_ENDPOINT;
    delete process.env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT;
    const { provider } = await freshTrace();
    const built = provider([new SimpleSpanProcessor(new InMemorySpanExporter())]);
    assert.equal(built._activeSpanProcessor["_spanProcessors"].length, 1);
  });

  test("with an endpoint, exactly one exporter is attached alongside the test processor", async () => {
    process.env.OTEL_EXPORTER_OTLP_ENDPOINT = "http://127.0.0.1:4318";
    try {
      const { provider } = await freshTrace();
      const built = provider([new SimpleSpanProcessor(new InMemorySpanExporter())]);
      assert.equal(built._activeSpanProcessor["_spanProcessors"].length, 2);
    } finally {
      delete process.env.OTEL_EXPORTER_OTLP_ENDPOINT;
    }
  });

  test("with OTEL_SERVICE_NAME unset, the resource says ziftbook-web", async () => {
    delete process.env.OTEL_SERVICE_NAME;
    delete process.env.OTEL_EXPORTER_OTLP_ENDPOINT;
    const { provider } = await freshTrace();
    const built = provider();
    const resource = built["_resource"];
    await resource.waitForAsyncAttributes?.();
    assert.equal(resource.attributes["service.name"], "ziftbook-web");
    assert.equal(process.env.OTEL_SERVICE_NAME, "ziftbook-web");
  });

  // W2 (guard, can't go red: the private provider has no context manager to inherit from).
  test("a proxy span is always a root, even with a current context", async () => {
    delete process.env.OTEL_EXPORTER_OTLP_ENDPOINT;
    const { provider, startProxySpan } = await freshTrace();
    provider();
    const outer = startProxySpan("GET");
    const inner = startProxySpan("POST");
    assert.notEqual(inner.spanContext().traceId, outer.spanContext().traceId);
    assert.equal(inner.parentSpanContext, undefined);
  });
});
