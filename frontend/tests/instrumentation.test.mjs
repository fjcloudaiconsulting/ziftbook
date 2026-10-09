// instrumentation.ts onRequestError: ziftbook.web.request.errors carries the route template only.
import assert from "node:assert/strict";
import { test } from "node:test";
import { AggregationTemporality, InMemoryMetricExporter, PeriodicExportingMetricReader } from "@opentelemetry/sdk-metrics";

import { onRequestError } from "../instrumentation.ts";
import { meterProvider } from "../lib/trace.ts";

test("F4: a page error counts once by route template; the request path never reaches the export", async () => {
  const exporter = new InMemoryMetricExporter(AggregationTemporality.DELTA);
  const reader = new PeriodicExportingMetricReader({ exporter, exportIntervalMillis: 3_600_000 });
  meterProvider([reader]);
  onRequestError(
    new Error("boom"),
    { path: "/nl/x?e=a@b.c", method: "GET", headers: {} },
    { routePath: "/[locale]/x", routeType: "render" },
  );
  await reader.forceFlush();
  const metrics = exporter.getMetrics().flatMap((rm) => rm.scopeMetrics).flatMap((sm) => sm.metrics);
  const points = metrics.filter((m) => m.descriptor.name === "ziftbook.web.request.errors").flatMap((m) => m.dataPoints);
  assert.equal(points.length, 1);
  assert.deepEqual(points[0].attributes, { "http.route": "/[locale]/x" });
  assert.equal(points[0].value, 1);
  assert.doesNotMatch(JSON.stringify(exporter.getMetrics()), /@/);
});
