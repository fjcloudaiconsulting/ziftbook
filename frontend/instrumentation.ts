// Runs once per server process, before any request. Startup logging and per-request error
// reporting, the same idea as backend/app/logs.py's configure() and app.main's exception handling.
import { errorFields, logger, parseFormat, parseLevel, requestId } from "./lib/log.ts";

export function register(): void {
  let level, format;
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
  logger("web.startup").info("started", { log_level: level, log_format: format });
}

type ErrorRequest = { path: string; method: string; headers: Record<string, string | string[] | undefined> };
type ErrorContext = { routePath: string; routeType: string };

export function onRequestError(err: unknown, request: ErrorRequest, context: ErrorContext): void {
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
  logger("web.request").error("request failed", fields);
}
