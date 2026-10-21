import { appVersion } from "../../lib/version.ts";

// Read per request: the console pages are prerendered at build time, where ZIF_SHOW_VERSION is unset,
// so the console fetches its version line from here (ZIF-158).
export const dynamic = "force-dynamic";

export function GET() {
  const version = appVersion();
  if (!version) return Response.json({ code: "not_found" }, { status: 404 });
  return Response.json(version, { headers: { "Cache-Control": "no-store" } });
}
