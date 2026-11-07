// Plain logic for the page that confirms a business's contact email from its link (ZIF-232), kept free of React
// so node --test can run it.

import { isInviteToken } from "./invite.ts";

/** Where the page starts: no link (reloaded, or opened without one), a mangled link, or one worth the button. The
 * link has the invite's shape: <business id>.<secrets.token_urlsafe(32)>. */
export function firstStage(token: string | null): "missing" | "invalid" | "ready" {
  if (token === null) return "missing";
  return isInviteToken(token) ? "ready" : "invalid";
}

/** What a confirm answer means for the page. */
export function confirmOutcome(status: number): "done" | "invalid" | "tooMany" | "other" {
  if (status === 204) return "done";
  if (status === 400) return "invalid";
  if (status === 429) return "tooMany";
  return "other";
}
