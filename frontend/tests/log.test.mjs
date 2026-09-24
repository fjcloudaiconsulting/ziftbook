// Structured logging for the web server, mirroring backend/app/logs.py's tests.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";
import { InMemoryLogRecordExporter, SimpleLogRecordProcessor } from "@opentelemetry/sdk-logs";

import { errorFields, format, logger, logProvider, parseFormat, parseLevel, requestErrorFields, requestId } from "../lib/log.ts";

// A developer's shell endpoint must never receive test lines: delete every OTEL_* var, then build
// the memoized provider with an in-memory exporter, no real network involved (ZIF-137 spec, test
// 13-16 setup).
for (const key of Object.keys(process.env)) {
  if (key.startsWith("OTEL_")) delete process.env[key];
}
const otlpExporter = new InMemoryLogRecordExporter();
logProvider([new SimpleLogRecordProcessor({ exporter: otlpExporter })]);

const HEAD_KEYS = ["ts", "level", "msg", "trace_id", "span_id"];

// The exported LogRecord, turned back into a line-shaped object, mirroring backend/tests/
// conftest.py's _rebuild: ts from the hrTime timestamp, level from severityText, msg from body,
// logger/extras/exc from attributes, trace_id/span_id from spanContext when present. Used to
// deepEqual against JSON.parse of the stdout line -- the same one-to-one parity fence as the
// backend (ZIF-137 spec, test 13).
function rebuild(rec) {
  const [seconds, nanos] = rec.hrTime;
  const ts = new Date(seconds * 1000 + Math.round(nanos / 1e6)).toISOString();
  // The SDK freezes array attribute values; round-trip through JSON so a rebuilt array compares
  // equal to the line's own array.
  const attributes = JSON.parse(JSON.stringify(rec.attributes ?? {}));
  for (const key of HEAD_KEYS) {
    assert.equal(key in attributes, false, `${key} must not stay in attributes`);
  }
  const fields = { ts, level: rec.severityText, msg: rec.body, ...attributes };
  if (rec.spanContext) {
    fields.trace_id = rec.spanContext.traceId;
    fields.span_id = rec.spanContext.spanId;
  }
  return fields;
}

describe("parseLevel (L1, L2)", () => {
  test("undefined and empty give INFO; the four values are accepted as-is", () => {
    assert.equal(parseLevel(undefined), "INFO");
    assert.equal(parseLevel(""), "INFO");
    for (const level of ["DEBUG", "INFO", "WARNING", "ERROR"]) {
      assert.equal(parseLevel(level), level);
    }
  });

  test("junk throws, case-sensitive, no fallback default", () => {
    for (const bad of ["debug", "Info", "TRACE"]) {
      assert.throws(() => parseLevel(bad));
    }
  });

  test("the thrown message names ZIF_LOG_LEVEL, never the rejected value (L2)", () => {
    assert.throws(() => parseLevel("verbose"), (err) => {
      assert.match(err.message, /ZIF_LOG_LEVEL/);
      assert.doesNotMatch(err.message, /verbose/);
      return true;
    });
  });
});

describe("parseFormat (L3)", () => {
  test("empty and undefined give json; text is accepted as-is; junk throws naming ZIF_LOG_FORMAT", () => {
    assert.equal(parseFormat(""), "json");
    assert.equal(parseFormat(undefined), "json");
    assert.equal(parseFormat("text"), "text");
    for (const bad of ["JSON", "pretty"]) {
      assert.throws(() => parseFormat(bad), (err) => {
        assert.match(err.message, /ZIF_LOG_FORMAT/);
        return true;
      });
    }
  });

  test("logger() under ZIF_LOG_FORMAT=text writes a non-JSON line starting with the ts", () => {
    process.env.ZIF_LOG_LEVEL = "INFO";
    process.env.ZIF_LOG_FORMAT = "text";
    const log = logger("web.test");
    const calls = [];
    const original = process.stdout.write;
    process.stdout.write = (chunk) => {
      calls.push(chunk);
      return true;
    };
    try {
      log.info("hello");
      assert.equal(calls.length, 1);
      const line = calls[0];
      assert.throws(() => JSON.parse(line), "a text-format line must not parse as JSON");
      assert.match(line, /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z /);
    } finally {
      process.stdout.write = original;
      delete process.env.ZIF_LOG_LEVEL;
      delete process.env.ZIF_LOG_FORMAT;
    }
  });
});

describe("format: json (L4)", () => {
  test("keys are exactly ts,level,logger,msg,+fields in order; fields never overwrite the head", () => {
    const now = new Date("2026-01-02T03:04:05.678Z");
    const line = format("json", "INFO", "web.proxy", "hi", { msg: "x", request_id: "r1", trace_id: undefined, gone: null }, now);
    const parsed = JSON.parse(line);
    assert.deepEqual(Object.keys(parsed), ["ts", "level", "logger", "msg", "request_id"]);
    assert.equal(parsed.ts, "2026-01-02T03:04:05.678Z");
    assert.match(parsed.ts, /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$/);
    assert.equal(parsed.msg, "hi", "a field named msg never overwrites the real message");
    assert.equal(parsed.request_id, "r1");
    assert.equal("trace_id" in parsed, false);
    assert.equal("gone" in parsed, false);
  });
});

describe("format: text (L5)", () => {
  test("msg and a field value with \\n and \\r come out escaped, no raw CR/LF", () => {
    const line = format("text", "ERROR", "web.request", "bad\nmsg\rhere", { note: "a\r\nb" });
    assert.doesNotMatch(line, /[\r\n]/);
    assert.match(line, /bad\\nmsg\\rhere/);
    assert.match(line, /note=a\\r\\nb/);
  });

  test("other control and line-separator characters also come out escaped, not raw", () => {
    const line = format("text", "ERROR", "web.request", "esc\x1btab", { note: "a\u2028b" });
    assert.doesNotMatch(line, /\x1b/);
    assert.match(line, /esc\\u001btab/);
    assert.doesNotMatch(line, /\u2028/);
    assert.match(line, /note=a\\u2028b/);
  });
});

describe("format: never throws (L6)", () => {
  test("a circular field in json: no throw, valid JSON with just the head", () => {
    const circular = {};
    circular.self = circular;
    const line = format("json", "INFO", "web.proxy", "hi", { circular });
    const parsed = JSON.parse(line);
    assert.equal(parsed.msg, "hi");
    assert.equal(parsed.level, "INFO");
    assert.equal(parsed.logger, "web.proxy");
    assert.equal("circular" in parsed, false);
  });

  test("a circular field in text: no throw", () => {
    const circular = {};
    circular.self = circular;
    assert.doesNotThrow(() => format("text", "INFO", "web.proxy", "hi", { circular }));
  });
});

describe("logger (L7)", () => {
  test("level filtering, reread from env at call time, process.stdout.write looked up at call time", () => {
    process.env.ZIF_LOG_LEVEL = "INFO";
    process.env.ZIF_LOG_FORMAT = "json";
    const log = logger("web.test");

    const calls = [];
    const original = process.stdout.write;
    process.stdout.write = (chunk) => {
      calls.push(chunk);
      return true;
    };
    try {
      log.debug("should not appear");
      assert.equal(calls.length, 0);
      log.error("should appear");
      assert.equal(calls.length, 1);
      assert.match(calls[0], /"msg":"should appear"/);

      process.env.ZIF_LOG_LEVEL = "ERROR";
      calls.length = 0;
      log.warning("should not appear either");
      assert.equal(calls.length, 0);
    } finally {
      process.stdout.write = original;
      delete process.env.ZIF_LOG_LEVEL;
      delete process.env.ZIF_LOG_FORMAT;
    }
  });

  test("a bad env value at log time falls back to INFO/json instead of throwing", () => {
    process.env.ZIF_LOG_LEVEL = "nope";
    delete process.env.ZIF_LOG_FORMAT;
    const calls = [];
    const original = process.stdout.write;
    process.stdout.write = (chunk) => {
      calls.push(chunk);
      return true;
    };
    try {
      assert.doesNotThrow(() => logger("x").error("m"));
      assert.equal(calls.length, 1, "startup has already refused this value; a log call must still write");
      assert.doesNotThrow(() => JSON.parse(calls[0]), "falls back to json, not text");
    } finally {
      process.stdout.write = original;
      delete process.env.ZIF_LOG_LEVEL;
    }
  });
});

describe("errorFields (L8)", () => {
  test("never leaks message text into the serialised output", () => {
    const err = new Error("a@b.c secret");
    const fields = errorFields(err);
    const serialised = JSON.stringify(fields);
    assert.doesNotMatch(serialised, /a@b\.c/);
    assert.doesNotMatch(serialised, /secret/);
    assert.equal(fields.exc[0].type, "Error");
    assert.ok(fields.exc[0].frames.length >= 1);
  });

  test("a multi-line message: frames strip the header correctly, no xyz/token leak", () => {
    const err = new Error("oops\n    at a@b.c token=xyz");
    const fields = errorFields(err);
    const serialised = JSON.stringify(fields);
    assert.doesNotMatch(serialised, /a@b\.c/);
    assert.doesNotMatch(serialised, /xyz/);
  });

  test("err.code is included only when it matches the safe pattern", () => {
    const err = new Error("x");
    err.code = "a@b.c";
    const fields = errorFields(err);
    assert.equal("code" in fields.exc[0], false);
  });

  test("a non-Error value: type is typeof, no message-shaped content", () => {
    const fields = errorFields("a@b.c");
    assert.equal(fields.exc.length, 1);
    assert.equal(fields.exc[0].type, "string");
    assert.doesNotMatch(JSON.stringify(fields), /a@b\.c/);
  });

  test("a throwing stack getter never throws", () => {
    const err = new Error("x");
    Object.defineProperty(err, "stack", {
      get() {
        throw new Error("boom");
      },
    });
    assert.doesNotThrow(() => errorFields(err));
  });

  test("a cause with code ECONNREFUSED shows up as exc[1].code", () => {
    const cause = new Error("connect ECONNREFUSED");
    cause.code = "ECONNREFUSED";
    const err = new Error("fetch failed", { cause });
    const fields = errorFields(err);
    assert.equal(fields.exc[1].code, "ECONNREFUSED");
  });

  test("a self-cycle terminates", () => {
    const err = new Error("x");
    err.cause = err;
    let fields;
    assert.doesNotThrow(() => {
      fields = errorFields(err);
    });
    assert.equal(fields.exc.length, 1);
  });

  test("a 7-deep cause chain is capped at 5 entries", () => {
    let err = new Error("e0");
    for (let i = 1; i < 7; i++) err = new Error(`e${i}`, { cause: err });
    const fields = errorFields(err);
    assert.equal(fields.exc.length, 5);
  });

  test("a stack with 25 at-lines is capped at 20 frames", () => {
    const err = new Error("x");
    const atLines = Array.from({ length: 25 }, (_, i) => `    at f${i} (file.js:${i}:1)`).join("\n");
    err.stack = `Error: x\n${atLines}`;
    const fields = errorFields(err);
    assert.equal(fields.exc[0].frames.length, 20);
  });

  test("a DOMException's type is its name, e.g. TimeoutError", () => {
    const err = new DOMException("x", "TimeoutError");
    const fields = errorFields(err);
    assert.equal(fields.exc[0].type, "TimeoutError");
  });
});

describe("requestId (L9)", () => {
  const HEX32 = /^[0-9a-f]{32}$/;

  test("a valid id is kept as-is", () => {
    assert.equal(requestId("abc-1"), "abc-1");
  });

  test("invalid inputs each fall back to 32 hex chars", () => {
    for (const bad of ["a".repeat(65), "a b", "x\ny", "r1, r2", "", null]) {
      assert.match(requestId(bad), HEX32, JSON.stringify(bad));
    }
  });
});

describe("requestErrorFields (L10)", () => {
  test("digest, message and headers never leak; request_id and route do", () => {
    const err = new Error("a@b.c");
    err.digest = "NEXT_REDIRECT;replace;/x?token=t;307;";
    const fields = requestErrorFields(
      err,
      { path: "/en/x?token=t", method: "GET", headers: { cookie: "c=secret", "x-request-id": "r1" } },
      { routePath: "/[locale]/x", routeType: "render" },
    );
    const serialised = JSON.stringify(fields);
    assert.doesNotMatch(serialised, /token/);
    assert.doesNotMatch(serialised, /secret/);
    assert.doesNotMatch(serialised, /a@b\.c/);
    assert.equal("digest" in fields, false, "a non-numeric-hash digest (a navigation digest) is dropped");
    assert.equal(fields.request_id, "r1");
    assert.equal(fields.route, "/[locale]/x");

    const err2 = new Error("y");
    err2.digest = "12345";
    const fields2 = requestErrorFields(err2, { path: "/en", method: "GET", headers: {} }, { routePath: "/[locale]", routeType: "render" });
    assert.equal(fields2.digest, "12345");
  });
});

describe("OTLP export (L13, L14)", () => {
  test("(L13) an error with an email and a cause chain, then a circular field: export equals the json line 1:1, no email leak", () => {
    otlpExporter.reset();
    process.env.ZIF_LOG_LEVEL = "INFO";
    process.env.ZIF_LOG_FORMAT = "json";
    const calls = [];
    const original = process.stdout.write;
    process.stdout.write = (chunk) => {
      calls.push(chunk);
      return true;
    };
    try {
      const cause = new Error("root a@b.c");
      const err = new Error("boom a@b.c", { cause });
      const log = logger("web.test13");
      log.error("failed", { ...errorFields(err), request_id: "r1" });
      assert.equal(calls.length, 1);
      const parsed = JSON.parse(calls[0]);
      const rec = otlpExporter.getFinishedLogRecords().at(-1);
      assert.deepEqual(rebuild(rec), parsed);
      const serialised = JSON.stringify(rec.attributes) + rec.body;
      assert.doesNotMatch(serialised, /a@b\.c/);

      calls.length = 0;
      otlpExporter.reset();
      const circular = {};
      circular.self = circular;
      log.error("circ", { circular });
      const parsed2 = JSON.parse(calls[0]);
      const rec2 = otlpExporter.getFinishedLogRecords().at(-1);
      assert.deepEqual(rebuild(rec2), parsed2, "the circular fallback (head-only) is the same on both paths");
    } finally {
      process.stdout.write = original;
      delete process.env.ZIF_LOG_LEVEL;
      delete process.env.ZIF_LOG_FORMAT;
    }
  });

  test("(L14) valid hex trace_id/span_id become the native spanContext with no id attributes; a non-hex id stays an attribute", () => {
    otlpExporter.reset();
    process.env.ZIF_LOG_LEVEL = "INFO";
    process.env.ZIF_LOG_FORMAT = "json";
    const original = process.stdout.write;
    process.stdout.write = () => true;
    try {
      const log = logger("web.proxy");
      const traceId = "a".repeat(32);
      const spanId = "b".repeat(16);
      log.info("req", { trace_id: traceId, span_id: spanId });
      const rec = otlpExporter.getFinishedLogRecords().at(-1);
      assert.equal(rec.spanContext.traceId, traceId);
      assert.equal(rec.spanContext.spanId, spanId);
      assert.equal("trace_id" in rec.attributes, false);
      assert.equal("span_id" in rec.attributes, false);

      otlpExporter.reset();
      log.info("req2", { trace_id: "not-hex", span_id: spanId });
      const rec2 = otlpExporter.getFinishedLogRecords().at(-1);
      assert.equal(rec2.attributes.trace_id, "not-hex");
      assert.equal(rec2.attributes.span_id, spanId, "a non-hex trace_id keeps span_id an attribute too, no partial context");
    } finally {
      process.stdout.write = original;
      delete process.env.ZIF_LOG_LEVEL;
      delete process.env.ZIF_LOG_FORMAT;
    }
  });
});

describe("(L16 guard) below ZIF_LOG_LEVEL nothing is exported", () => {
  test("a filtered-out debug call never reaches the exporter", () => {
    otlpExporter.reset();
    process.env.ZIF_LOG_LEVEL = "ERROR";
    const original = process.stdout.write;
    process.stdout.write = () => true;
    try {
      logger("x").debug("nope");
      assert.equal(otlpExporter.getFinishedLogRecords().length, 0);
    } finally {
      process.stdout.write = original;
      delete process.env.ZIF_LOG_LEVEL;
    }
  });
});

describe("source guards (L11)", () => {
  const read = (rel) => readFileSync(new URL(rel, import.meta.url), "utf8");

  test("instrumentation.ts exports register and onRequestError as functions", () => {
    const src = read("../instrumentation.ts");
    assert.match(src, /export (async )?function register\b/);
    assert.match(src, /export (async )?function onRequestError\b/);
  });

  test("proxy.ts, instrumentation.ts and lib/log.ts never call console.*", () => {
    for (const rel of ["../proxy.ts", "../instrumentation.ts", "../lib/log.ts"]) {
      assert.doesNotMatch(read(rel), /\bconsole\./, rel);
    }
  });
});
