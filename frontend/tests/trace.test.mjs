// lib/trace.ts: the proxy's manual spans. W1 (table id) in docs/specs/zif-87-traces.md.
// provider() memoizes a single BasicTracerProvider per module instance, so each scenario that
// needs its own env/processors re-imports the module with a cache-busting query string.
import assert from "node:assert/strict";
import { describe, test } from "node:test";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { SpanStatusCode } from "@opentelemetry/api";
import { AggregationTemporality, InMemoryMetricExporter, PeriodicExportingMetricReader } from "@opentelemetry/sdk-metrics";

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

  // Review: traceparent() of a span with an invalid context returns undefined.
  test("traceparent of a span with an invalid context is undefined", async () => {
    delete process.env.OTEL_EXPORTER_OTLP_ENDPOINT;
    const { traceparent } = await freshTrace();
    const invalidSpan = { spanContext: () => ({ traceId: "0".repeat(32), spanId: "0".repeat(16), traceFlags: 0 }) };
    assert.equal(traceparent(invalidSpan), undefined);
  });

  // Review: endProxySpan sets error.type from the status when there's no error object.
  test("endProxySpan(span, 503) sets ERROR status with error.type \"503\"", async () => {
    delete process.env.OTEL_EXPORTER_OTLP_ENDPOINT;
    const { provider, startProxySpan, endProxySpan } = await freshTrace();
    provider();
    const span = startProxySpan("GET");
    endProxySpan(span, 503);
    assert.equal(span.status.code, SpanStatusCode.ERROR);
    assert.equal(span.attributes["error.type"], "503");
  });

  // Review: endProxySpan sets error.type from the error's class when one is given.
  test('endProxySpan(span, 502, new TypeError()) sets error.type "TypeError"', async () => {
    delete process.env.OTEL_EXPORTER_OTLP_ENDPOINT;
    const { provider, startProxySpan, endProxySpan } = await freshTrace();
    provider();
    const span = startProxySpan("GET");
    endProxySpan(span, 502, new TypeError());
    assert.equal(span.status.code, SpanStatusCode.ERROR);
    assert.equal(span.attributes["error.type"], "TypeError");
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

describe("trace export gate: enabled()", () => {
  const DEAD = "http://127.0.0.1:9";
  const clear = () => {
    for (const key of Object.keys(process.env)) if (key.startsWith("OTEL_")) delete process.env[key];
  };
  const gate = async (env) => {
    clear();
    Object.assign(process.env, env);
    try {
      return (await freshTrace()).enabled();
    } finally {
      clear();
    }
  };

  test("only the traces-specific endpoint turns it on", async () => {
    assert.equal(await gate({ OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: DEAD }), true);
  });
  test("a logs-only endpoint does not", async () => {
    assert.equal(await gate({ OTEL_EXPORTER_OTLP_LOGS_ENDPOINT: DEAD }), false);
  });
  test("a whitespace-only endpoint is off", async () => {
    assert.equal(await gate({ OTEL_EXPORTER_OTLP_ENDPOINT: "   " }), false);
  });
  test('OTEL_TRACES_EXPORTER "none", " none " and "NONE" are off with an endpoint set', async () => {
    for (const v of ["none", " none ", "NONE"]) {
      assert.equal(await gate({ OTEL_EXPORTER_OTLP_ENDPOINT: DEAD, OTEL_TRACES_EXPORTER: v }), false, v);
    }
  });
  test("an empty traces endpoint falls back to the generic one", async () => {
    assert.equal(await gate({ OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: "", OTEL_EXPORTER_OTLP_ENDPOINT: DEAD }), true);
  });
});

describe("proxy request metrics (ZIF-88)", () => {
  const DEAD = "http://127.0.0.1:9";
  const clear = () => {
    for (const key of Object.keys(process.env)) if (key.startsWith("OTEL_")) delete process.env[key];
  };
  const harness = async () => {
    clear();
    const trace = await freshTrace();
    const exporter = new InMemoryMetricExporter(AggregationTemporality.DELTA);
    const reader = new PeriodicExportingMetricReader({ exporter, exportIntervalMillis: 3_600_000 });
    trace.meterProvider([reader]);
    trace.provider();
    const points = async (name) => {
      await reader.forceFlush();
      const found = exporter
        .getMetrics()
        .flatMap((rm) => rm.scopeMetrics)
        .flatMap((sm) => sm.metrics)
        .filter((m) => m.descriptor.name === name)
        .flatMap((m) => m.dataPoints);
      exporter.reset();
      return found;
    };
    return { ...trace, points };
  };

  test("F1: a 2xx ends with one point whose keys are exactly the three; a 502 adds error.type", async () => {
    const { startProxySpan, endProxySpan, points } = await harness();
    endProxySpan(startProxySpan("GET"), 200);
    const ok = await points("http.server.request.duration");
    assert.equal(ok.length, 1);
    assert.deepEqual(Object.keys(ok[0].attributes).sort(), ["http.request.method", "http.response.status_code", "http.route"]);
    assert.equal(ok[0].attributes["http.route"], "/api/:path*");
    assert.equal(ok[0].attributes["http.response.status_code"], 200);
    assert.equal(ok[0].value.count, 1);

    endProxySpan(startProxySpan("GET"), 502, new TypeError());
    const bad = await points("http.server.request.duration");
    assert.equal(bad.length, 1);
    assert.deepEqual(Object.keys(bad[0].attributes).sort(), [
      "error.type",
      "http.request.method",
      "http.response.status_code",
      "http.route",
    ]);
    assert.equal(bad[0].attributes["error.type"], "TypeError");
  });

  test("F2: an unknown method is recorded as _OTHER, a standard one as itself", async () => {
    const { startProxySpan, endProxySpan, points } = await harness();
    endProxySpan(startProxySpan("FOO"), 200);
    endProxySpan(startProxySpan("PATCH"), 200);
    const methods = (await points("http.server.request.duration")).map((p) => p.attributes["http.request.method"]).sort();
    assert.deepEqual(methods, ["PATCH", "_OTHER"]);
  });

  test("F3: no endpoint, or OTEL_METRICS_EXPORTER=none, attaches no reader of its own", async () => {
    const count = async (env) => {
      clear();
      Object.assign(process.env, env);
      try {
        const { meterProvider } = await freshTrace();
        return meterProvider()["_sharedState"].metricCollectors.length;
      } finally {
        clear();
      }
    };
    assert.equal(await count({}), 0);
    assert.equal(await count({ OTEL_EXPORTER_OTLP_ENDPOINT: DEAD, OTEL_METRICS_EXPORTER: "none" }), 0);
    assert.equal(await count({ OTEL_EXPORTER_OTLP_ENDPOINT: DEAD }), 1);
  });
});
