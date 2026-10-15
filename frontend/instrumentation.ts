// Next's instrumentation hooks. Next also compiles this file for its Edge runtime, where the logger's
// Node-only APIs (node:crypto, process.stdout) would warn on every compile, so the real code loads
// through a dynamic import only when NEXT_RUNTIME is "nodejs" (ZIF-141). The app never runs on Edge.
import type { ErrorContext, ErrorRequest } from "./lib/log.ts";

export async function register(): Promise<void> {
  if (process.env.NEXT_RUNTIME === "nodejs") (await import("./instrumentation-node.ts")).register();
}

export async function onRequestError(err: unknown, request: ErrorRequest, context: ErrorContext): Promise<void> {
  if (process.env.NEXT_RUNTIME === "nodejs") (await import("./instrumentation-node.ts")).onRequestError(err, request, context);
}
