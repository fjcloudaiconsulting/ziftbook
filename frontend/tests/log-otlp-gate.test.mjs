// lib/log.ts and lib/trace.ts each gate their OTLP exporter on the same rule, duplicated rather
// than shared (lib/ files never import a sibling -- see lib/log.ts's header comment). Both copies
// need their own coverage here, or drift between them goes undetected (ZIF-137 spec, test 15/16).
// Each scenario re-imports both modules fresh (their providers are memoized module state) with a
// cache-busting query string, the same pattern as tests/trace.test.mjs.
import assert from "node:assert/strict";
import { describe, test } from "node:test";

const LOG_PATH = "../lib/log.ts";
const TRACE_PATH = "../lib/trace.ts";
const DEAD_ENDPOINT = "http://127.0.0.1:9";

let seq = 0;
function fresh(path) {
  seq += 1;
  return import(`${path}?case=${seq}`);
}

function clearOtelEnv() {
  for (const key of Object.keys(process.env)) {
    if (key.startsWith("OTEL_")) delete process.env[key];
  }
}

describe("OTLP export gates (L15)", () => {
  test("only OTEL_EXPORTER_OTLP_LOGS_ENDPOINT: log.ts is on, trace.ts is off", async () => {
    clearOtelEnv();
    process.env.OTEL_EXPORTER_OTLP_LOGS_ENDPOINT = DEAD_ENDPOINT;
    try {
      const { logProvider } = await fresh(LOG_PATH);
      const built = logProvider();
      assert.equal(built.processors.length, 1);
      await built.provider.shutdown();

      const { enabled } = await fresh(TRACE_PATH);
      assert.equal(enabled(), false);
    } finally {
      clearOtelEnv();
    }
  });

  test("only OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: trace.ts is on, log.ts is off", async () => {
    clearOtelEnv();
    process.env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT = DEAD_ENDPOINT;
    try {
      const { logProvider } = await fresh(LOG_PATH);
      const built = logProvider();
      assert.equal(built.processors.length, 0);
      await built.provider.shutdown();

      const { enabled } = await fresh(TRACE_PATH);
      assert.equal(enabled(), true);
    } finally {
      clearOtelEnv();
    }
  });

  test('OTEL_LOGS_EXPORTER="NONE" (case-insensitive) with a generic endpoint set: log.ts is off, trace.ts stays on', async () => {
    clearOtelEnv();
    process.env.OTEL_EXPORTER_OTLP_ENDPOINT = DEAD_ENDPOINT;
    process.env.OTEL_LOGS_EXPORTER = "NONE";
    try {
      const { logProvider } = await fresh(LOG_PATH);
      const built = logProvider();
      assert.equal(built.processors.length, 0);
      await built.provider.shutdown();

      const { enabled } = await fresh(TRACE_PATH);
      assert.equal(enabled(), true);
    } finally {
      clearOtelEnv();
    }
  });

  test("a whitespace-only endpoint is off for both log.ts and trace.ts", async () => {
    clearOtelEnv();
    process.env.OTEL_EXPORTER_OTLP_ENDPOINT = "   ";
    try {
      const { logProvider } = await fresh(LOG_PATH);
      const built = logProvider();
      assert.equal(built.processors.length, 0);
      await built.provider.shutdown();

      const { enabled } = await fresh(TRACE_PATH);
      assert.equal(enabled(), false);
    } finally {
      clearOtelEnv();
    }
  });

  test('a " none " (whitespace around the value) OTEL_LOGS_EXPORTER is still off, not just an exact "none"', async () => {
    clearOtelEnv();
    process.env.OTEL_EXPORTER_OTLP_ENDPOINT = DEAD_ENDPOINT;
    process.env.OTEL_LOGS_EXPORTER = " none ";
    try {
      const { logProvider } = await fresh(LOG_PATH);
      const built = logProvider();
      assert.equal(built.processors.length, 0);
      await built.provider.shutdown();
    } finally {
      clearOtelEnv();
    }
  });

  test('a " none " (whitespace around the value) OTEL_TRACES_EXPORTER is still off, not just an exact "none"', async () => {
    clearOtelEnv();
    process.env.OTEL_EXPORTER_OTLP_ENDPOINT = DEAD_ENDPOINT;
    process.env.OTEL_TRACES_EXPORTER = " none ";
    try {
      const { enabled } = await fresh(TRACE_PATH);
      assert.equal(enabled(), false);
    } finally {
      clearOtelEnv();
    }
  });

  test("OTEL_TRACES_EXPORTER=none: trace.ts's enabled() is false while log.ts still builds its exporter", async () => {
    clearOtelEnv();
    process.env.OTEL_EXPORTER_OTLP_ENDPOINT = DEAD_ENDPOINT;
    process.env.OTEL_TRACES_EXPORTER = "none";
    try {
      const { enabled } = await fresh(TRACE_PATH);
      assert.equal(enabled(), false);

      const { logProvider } = await fresh(LOG_PATH);
      const built = logProvider();
      assert.equal(built.processors.length, 1);
      await built.provider.shutdown();
    } finally {
      clearOtelEnv();
    }
  });

  test("mirror: OTEL_LOGS_EXPORTER=none leaves log.ts off while trace.ts still builds its exporter", async () => {
    clearOtelEnv();
    process.env.OTEL_EXPORTER_OTLP_ENDPOINT = DEAD_ENDPOINT;
    process.env.OTEL_LOGS_EXPORTER = "none";
    try {
      const { logProvider } = await fresh(LOG_PATH);
      const built = logProvider();
      assert.equal(built.processors.length, 0);
      await built.provider.shutdown();

      const { enabled } = await fresh(TRACE_PATH);
      assert.equal(enabled(), true);
    } finally {
      clearOtelEnv();
    }
  });

  test("an empty signal-specific endpoint falls back to the generic one (log.ts)", async () => {
    clearOtelEnv();
    process.env.OTEL_EXPORTER_OTLP_LOGS_ENDPOINT = "";
    process.env.OTEL_EXPORTER_OTLP_ENDPOINT = DEAD_ENDPOINT;
    try {
      const { logProvider } = await fresh(LOG_PATH);
      const built = logProvider();
      assert.equal(built.processors.length, 1);
      await built.provider.shutdown();
    } finally {
      clearOtelEnv();
    }
  });

  test("an empty signal-specific endpoint falls back to the generic one (trace.ts)", async () => {
    clearOtelEnv();
    process.env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT = "";
    process.env.OTEL_EXPORTER_OTLP_ENDPOINT = DEAD_ENDPOINT;
    try {
      const { enabled } = await fresh(TRACE_PATH);
      assert.equal(enabled(), true);
    } finally {
      clearOtelEnv();
    }
  });
});

describe("(L16 guard) a throwing log processor never throws out of logger().error", () => {
  test("stdout is still written when the export processor's onEmit throws", async () => {
    clearOtelEnv();
    const { logProvider, logger } = await fresh(LOG_PATH);
    const built = logProvider([
      {
        onEmit() {
          throw new Error("boom");
        },
        forceFlush: async () => {},
        shutdown: async () => {},
      },
    ]);
    assert.equal(built.processors.length, 1);
    const calls = [];
    const original = process.stdout.write;
    process.stdout.write = (chunk) => {
      calls.push(chunk);
      return true;
    };
    try {
      assert.doesNotThrow(() => logger("x").error("m"));
      assert.equal(calls.length, 1, "the stdout line is still written even though the processor throws");
    } finally {
      process.stdout.write = original;
    }
  });
});
