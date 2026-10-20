import { paymentsDashboard } from "@/api-client";

import type { useConsole } from "./console";
import { JSON_WRITE } from "./parts";

/** Opens the business's Stripe dashboard in a new tab. The tab is made inside the click, before the
 * await, or the browser blocks it as a popup; if it was blocked anyway this page goes there. `guard`
 * is the caller's busy flag: a second click while the link is being fetched does nothing. */
export async function openStripeDashboard(
  call: ReturnType<typeof useConsole>["call"],
  guard: { current: boolean },
  setBusy: (busy: boolean) => void,
): Promise<"ok" | "signedOut" | "failed" | "busy"> {
  if (guard.current) return "busy";
  guard.current = true;
  setBusy(true);
  const tab = window.open("", "_blank");
  if (tab) tab.opener = null;
  const outcome = await call(() => paymentsDashboard(JSON_WRITE), { write: true });
  // Reset even before leaving for Stripe: Back may restore this page from the bfcache as it was.
  guard.current = false;
  setBusy(false);
  if (outcome.status === 200 && outcome.data) {
    if (tab) tab.location.href = outcome.data.url;
    else window.location.assign(outcome.data.url);
    return "ok";
  }
  tab?.close();
  return outcome.status === 401 ? "signedOut" : "failed";
}
