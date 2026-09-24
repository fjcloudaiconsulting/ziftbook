// Runs once per server process, before any request. Startup logging and per-request error
// reporting, the same idea as backend/app/logs.py's configure() and app.main's exception handling.
// Field-building lives in lib/log.ts (pure, so it's testable without a logger); this file only
// wires it to Next's lifecycle hooks.
import {
  type ErrorContext,
  type ErrorRequest,
  type Format,
  type Level,
  logger,
  parseFormat,
  parseLevel,
  requestErrorFields,
} from "./lib/log";

const startupLog = logger("web.startup");
const requestLog = logger("web.request");

export function register(): void {
  let level: Level, format: Format;
  try {
    level = parseLevel(process.env.ZIF_LOG_LEVEL);
  } catch (err) {
    process.stderr.write(`invalid ${err instanceof Error ? err.message : "ZIF_LOG_LEVEL"}\n`);
    process.exit(1);
  }
  try {
    format = parseFormat(process.env.ZIF_LOG_FORMAT);
  } catch (err) {
    process.stderr.write(`invalid ${err instanceof Error ? err.message : "ZIF_LOG_FORMAT"}\n`);
    process.exit(1);
  }
  startupLog.info("started", { log_level: level, log_format: format });
}

export function onRequestError(err: unknown, request: ErrorRequest, context: ErrorContext): void {
  requestLog.error("request failed", requestErrorFields(err, request, context));
}
