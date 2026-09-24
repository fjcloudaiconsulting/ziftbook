// Structured, levelled logs for the web server, mirroring backend/app/logs.py: one line per call
// (JSON or text), ids only, never an error's message. Env is read at call time (like apiUrl()), so
// one built image works in dev or prod, and a bad value never gets silently swapped for a default.
//
// No sibling import of lib/trace.ts here (see docs/specs/2026-09-24-zif-137-spec.md): "./trace"
// fails ERR_MODULE_NOT_FOUND under node --test, and "./trace.ts" fails typecheck (TS5097,
// allowImportingTsExtensions stays off per ZIF-50). So the gate below is a private duplicate of
// lib/trace.ts's enabled(), imports npm packages only.
import { randomUUID } from "node:crypto";
import { ROOT_CONTEXT, trace } from "@opentelemetry/api";
import { OTLPLogExporter } from "@opentelemetry/exporter-logs-otlp-http";
import { detectResources, envDetector } from "@opentelemetry/resources";
import { BatchLogRecordProcessor, LoggerProvider, type LogRecordProcessor } from "@opentelemetry/sdk-logs";

const LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"] as const;
export type Level = (typeof LEVELS)[number];
const ORDER: Record<Level, number> = { DEBUG: 0, INFO: 1, WARNING: 2, ERROR: 3 };

const FORMATS = ["json", "text"] as const;
export type Format = (typeof FORMATS)[number];

export function parseLevel(value: string | undefined): Level {
  if (value === undefined || value === "") return "INFO";
  if ((LEVELS as readonly string[]).includes(value)) return value as Level;
  throw new Error("ZIF_LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR");
}

export function parseFormat(value: string | undefined): Format {
  if (value === undefined || value === "") return "json";
  if ((FORMATS as readonly string[]).includes(value)) return value as Format;
  throw new Error("ZIF_LOG_FORMAT must be one of json, text");
}

const RESERVED = new Set(["ts", "level", "logger", "msg"]);

// The gate, duplicated from lib/trace.ts (never imported, see the header comment): an endpoint
// (OTEL_EXPORTER_OTLP_LOGS_ENDPOINT or OTEL_EXPORTER_OTLP_ENDPOINT, trimmed) and
// OTEL_LOGS_EXPORTER, trimmed and lower-cased, not "none".
function enabled(): boolean {
  const endpoint = (process.env.OTEL_EXPORTER_OTLP_LOGS_ENDPOINT ?? process.env.OTEL_EXPORTER_OTLP_ENDPOINT ?? "").trim();
  if (!endpoint) return false;
  const exporter = (process.env.OTEL_LOGS_EXPORTER ?? "").trim().toLowerCase();
  return exporter !== "none";
}

let built: { provider: LoggerProvider; processors: LogRecordProcessor[] } | undefined;

// Lazy and memoized: built once, on first use, never registered globally, the same approach as
// lib/trace.ts's provider(). Returns the processor array it built so a test can see whether the
// OTLP exporter was added.
export function logProvider(processors: LogRecordProcessor[] = []): { provider: LoggerProvider; processors: LogRecordProcessor[] } {
  if (built) return built;
  process.env.OTEL_SERVICE_NAME ??= "ziftbook-web";
  const logProcessors: LogRecordProcessor[] = [...processors];
  if (enabled()) logProcessors.push(new BatchLogRecordProcessor({ exporter: new OTLPLogExporter() }));
  const provider = new LoggerProvider({
    resource: detectResources({ detectors: [envDetector] }),
    processors: logProcessors,
  });
  built = { provider, processors: logProcessors };
  return built;
}

const HEX32 = /^[0-9a-f]{32}$/;
const HEX16 = /^[0-9a-f]{16}$/;
// Inlined rather than importing @opentelemetry/api-logs' SeverityNumber (see the spec: not a
// direct dependency). Only the four levels lib/log.ts's logger() emits ever reach here.
const SEVERITY_NUMBER: Record<Level, number> = { DEBUG: 5, INFO: 9, WARNING: 13, ERROR: 17 };

// Maps the parsed JSON line per docs/specs/2026-09-24-zif-137-spec.md's field-mapping table and
// emits it. Not exported: only write() calls it.
function exportLine(rec: Record<string, unknown>): void {
  const { processors } = logProvider();
  if (processors.length === 0) return;
  const { ts, level, msg, trace_id, span_id, ...rest } = rec;
  const attributes: Record<string, unknown> = { ...rest };
  let context = ROOT_CONTEXT;
  const traceId = typeof trace_id === "string" ? trace_id : undefined;
  const spanId = typeof span_id === "string" ? span_id : undefined;
  if (traceId !== undefined && HEX32.test(traceId) && spanId !== undefined && HEX16.test(spanId)) {
    context = trace.setSpanContext(ROOT_CONTEXT, { traceId, spanId, traceFlags: 0 });
  } else {
    if (trace_id !== undefined) attributes.trace_id = trace_id;
    if (span_id !== undefined) attributes.span_id = span_id;
  }
  const emitter = logProvider().provider.getLogger("ziftbook-web");
  // The record came from JSON.parse (arbitrary JSON, untyped), not from api-logs' own types
  // (not a direct dependency -- see the spec -- so its types aren't imported either); the emit
  // parameter type is taken structurally from `emitter` itself instead.
  type EmitArg = Parameters<typeof emitter.emit>[0];
  emitter.emit({
    timestamp: typeof ts === "string" ? new Date(ts) : new Date(),
    severityNumber: SEVERITY_NUMBER[level as Level] ?? 0,
    severityText: level as string,
    body: msg,
    attributes,
    context,
  } as EmitArg);
}

// Stricter than backend/app/logs.py's _escape, which only escapes \r and \n: those two could
// split a text line, but every other control byte (plus the Unicode line/paragraph separators,
// which some renderers treat as a newline) could still inject one, so all of them get a \uXXXX
// escape instead of passing through.
function escape(value: string): string {
  return value
    .replace(/\r/g, "\\r")
    .replace(/\n/g, "\\n")
    .replace(/[\x00-\x1f\x7f-\x9f\u2028\u2029]/g, (c) => `\\u${c.codePointAt(0)!.toString(16).padStart(4, "0")}`);
}

export function format(
  kind: Format,
  level: Level,
  logger: string,
  msg: string,
  fields: Record<string, unknown> = {},
  now: Date = new Date(),
): string {
  const ts = now.toISOString();
  const headLine = `${ts} ${level} ${logger}: ${escape(msg)}`;
  const headOnly = kind === "json" ? JSON.stringify({ ts, level, logger, msg }) : headLine;
  try {
    const rest: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(fields)) {
      if (RESERVED.has(key) || value === undefined || value === null) continue;
      rest[key] = value;
    }
    if (kind === "json") return JSON.stringify({ ts, level, logger, msg, ...rest });
    let line = headLine;
    for (const [key, value] of Object.entries(rest)) {
      const text = typeof value === "string" ? value : JSON.stringify(value);
      line += ` ${key}=${escape(text)}`;
    }
    return line;
  } catch {
    return headOnly;
  }
}

export function logger(name: string): {
  debug: (msg: string, fields?: Record<string, unknown>) => void;
  info: (msg: string, fields?: Record<string, unknown>) => void;
  warning: (msg: string, fields?: Record<string, unknown>) => void;
  error: (msg: string, fields?: Record<string, unknown>) => void;
} {
  const write = (level: Level, msg: string, fields?: Record<string, unknown>) => {
    let level_: Level, format_: Format;
    try {
      level_ = parseLevel(process.env.ZIF_LOG_LEVEL);
      format_ = parseFormat(process.env.ZIF_LOG_FORMAT);
    } catch {
      level_ = "INFO";
      format_ = "json";
    }
    if (ORDER[level] < ORDER[level_]) return;
    const now = new Date();
    const line = format(format_, level, name, msg, fields, now);
    process.stdout.write(line + "\n");
    try {
      exportLine(JSON.parse(format_ === "json" ? line : format("json", level, name, msg, fields, now)));
    } catch {
      // Logging never throws.
    }
  };
  return {
    debug: (msg, fields) => write("DEBUG", msg, fields),
    info: (msg, fields) => write("INFO", msg, fields),
    warning: (msg, fields) => write("WARNING", msg, fields),
    error: (msg, fields) => write("ERROR", msg, fields),
  };
}

type ExcLink = { type: string; code?: string; frames: string[] };

function errorType(e: unknown): string {
  if (e instanceof DOMException) return e.name;
  if (e instanceof Error) return e.constructor?.name || "Error";
  return typeof e;
}

function framesOf(e: unknown): string[] {
  if (!(e instanceof Error)) return [];
  const stack = e.stack;
  if (typeof stack !== "string") return [];
  const prefix = `${e.name}: ${e.message}`;
  if (!stack.startsWith(prefix)) return [];
  return stack
    .slice(prefix.length)
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => /^at /.test(line))
    .slice(0, 20);
}

function errorLink(e: unknown): ExcLink {
  const link: ExcLink = { type: errorType(e), frames: [] };
  try {
    const code = (e as { code?: unknown } | null)?.code;
    if (typeof code === "string" && /^[A-Z0-9_]{1,32}$/.test(code)) link.code = code;
  } catch {
    // leave code absent
  }
  try {
    link.frames = framesOf(e);
  } catch {
    link.frames = [];
  }
  return link;
}

/** Mirrors backend/app/logs.py's exc chain: one entry per error along err.cause, at most 5,
 * cycle-safe. Never err.message, never String(err): either can quote an address or a token. */
export function errorFields(err: unknown): { exc: ExcLink[] } {
  const exc: ExcLink[] = [];
  try {
    const seen = new Set<unknown>();
    let current: unknown = err;
    while (current !== null && current !== undefined && exc.length < 5 && !seen.has(current)) {
      seen.add(current);
      exc.push(errorLink(current));
      let next: unknown;
      try {
        next = current instanceof Error ? current.cause : undefined;
      } catch {
        next = undefined;
      }
      current = next;
    }
  } catch {
    // return whatever was collected before the failure
  }
  return { exc };
}

const REQUEST_ID = /^[A-Za-z0-9._:-]{1,64}$/;

export function requestId(header: string | null): string {
  if (header !== null && REQUEST_ID.test(header)) return header;
  return randomUUID().replaceAll("-", "");
}

export type ErrorRequest = { path: string; method: string; headers: Record<string, string | string[] | undefined> };
export type ErrorContext = { routePath: string; routeType: string };

/** The fields for instrumentation.ts's onRequestError log line, as a pure function so a test can
 * check them without going through a logger or importing instrumentation.ts itself. Never
 * request.path (carries the query string) or any header but x-request-id. */
export function requestErrorFields(err: unknown, request: ErrorRequest, context: ErrorContext): Record<string, unknown> {
  const header = request.headers["x-request-id"];
  const fields: Record<string, unknown> = {
    request_id: requestId(typeof header === "string" ? header : null),
    method: request.method,
    route: context.routePath,
    route_type: context.routeType,
  };
  let digest: unknown;
  try {
    digest = (err as { digest?: unknown } | null)?.digest;
  } catch {
    digest = undefined;
  }
  if (typeof digest === "string" && /^\d{1,20}$/.test(digest)) fields.digest = digest;
  Object.assign(fields, errorFields(err));
  return fields;
}
