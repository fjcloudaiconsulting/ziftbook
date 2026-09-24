// Structured, levelled logs for the web server, mirroring backend/app/logs.py: one line per call
// (JSON or text), ids only, never an error's message. Env is read at call time (like apiUrl()), so
// one built image works in dev or prod, and a bad value never gets silently swapped for a default.
import { randomUUID } from "node:crypto";

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

// mirrors backend/app/logs.py's _escape: \r and \n could split a text line, and every other
// control byte (plus the Unicode line/paragraph separators, which some renderers treat as a
// newline) could still inject one, so all of them get a \uXXXX escape instead of passing through.
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
    process.stdout.write(format(format_, level, name, msg, fields) + "\n");
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
