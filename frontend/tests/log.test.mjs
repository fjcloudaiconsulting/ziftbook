// Structured logging for the web server, mirroring backend/app/logs.py's tests.
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { errorFields, format, logger, parseFormat, parseLevel, requestId } from "../lib/log.ts";

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
  test("empty gives json; junk throws naming ZIF_LOG_FORMAT", () => {
    assert.equal(parseFormat(""), "json");
    for (const bad of ["JSON", "pretty"]) {
      assert.throws(() => parseFormat(bad), (err) => {
        assert.match(err.message, /ZIF_LOG_FORMAT/);
        return true;
      });
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

describe("onRequestError-shaped fields (L10)", () => {
  test("digest, message and headers never leak; request_id and route do", async () => {
    const { onRequestError } = await import("../instrumentation.ts");
    const calls = [];
    const original = process.stdout.write;
    process.stdout.write = (chunk) => {
      calls.push(chunk);
      return true;
    };
    process.env.ZIF_LOG_LEVEL = "INFO";
    process.env.ZIF_LOG_FORMAT = "json";
    try {
      const err = new Error("a@b.c");
      err.digest = "NEXT_REDIRECT;replace;/x?token=t;307;";
      await onRequestError(
        err,
        { path: "/en/x?token=t", method: "GET", headers: { cookie: "c=secret", "x-request-id": "r1" } },
        { routePath: "/[locale]/x", routeType: "render" },
      );
      assert.equal(calls.length, 1);
      const line = calls[0];
      assert.doesNotMatch(line, /token/);
      assert.doesNotMatch(line, /secret/);
      assert.doesNotMatch(line, /a@b\.c/);
      assert.doesNotMatch(line, /digest/);
      const parsed = JSON.parse(line);
      assert.equal(parsed.request_id, "r1");
      assert.equal(parsed.route, "/[locale]/x");

      calls.length = 0;
      const err2 = new Error("y");
      err2.digest = "12345";
      await onRequestError(err2, { path: "/en", method: "GET", headers: {} }, { routePath: "/[locale]", routeType: "render" });
      const parsed2 = JSON.parse(calls[0]);
      assert.equal(parsed2.digest, "12345");
    } finally {
      process.stdout.write = original;
      delete process.env.ZIF_LOG_LEVEL;
      delete process.env.ZIF_LOG_FORMAT;
    }
  });
});
