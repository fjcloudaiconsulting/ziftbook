// ZIF-146: plain logic for a client's own ziftbook account (no business), kept free of React so node --test can
// run it. Nothing here looks anyone up: every answer is about the person signed in on this browser.

import { signInOutcome } from "./account.ts";

/** What an answer to `POST /api/account/session` means for the form. A wrong password is the console's generic
 * answer (`SignIn.invalid`); `account_unavailable` (a platform admin) only follows a correct password. */
export function accountSignInOutcome(
  status: number,
  code: string | undefined,
  hasSiteKey: boolean,
): "ok" | "invalid" | "unavailable" | "challenge" | "try_later" | "error" {
  if (status === 403 && code === "account_unavailable") return "unavailable";
  return signInOutcome(status, code, hasSiteKey);
}
