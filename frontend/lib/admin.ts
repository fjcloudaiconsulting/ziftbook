// Plain logic for the admin sign-in screens, kept free of React so node --test can run it.

/** Where the session's `second_factor` sends the browser (ZIF-71 spec section 4). Anything unknown is not "passed". */
export function stepFor(secondFactor: string): "done" | "ticket" | "code" {
  if (secondFactor === "passed") return "done";
  return secondFactor === "enrolment_required" ? "ticket" : "code";
}

/** The manual key in groups of four, to be read off the screen. */
export function secretGroups(secret: string): string[] {
  return secret.match(/.{1,4}/g) ?? [];
}

/** The .txt a user saves: a header, a blank line, then the numbered codes. */
export function recoveryFile(header: string, codes: string[]): string {
  return `${header}\n\n${codes.map((code, i) => `${i + 1}. ${code}`).join("\n")}\n`;
}

export type FactorState = "wrong" | "locked" | "unavailable" | "tooMany" | "conflict" | "expired" | "other";

/**
 * What a second-factor or enrolment answer means (spec section 1 error codes). Codes win over statuses because
 * 401 is both a wrong code and a dead session; a 409 means something different per route, so the caller reads it.
 */
export function factorState(status: number, code?: string): FactorState {
  if (code === "invalid_code" || code === "invalid_ticket") return "wrong";
  if (code === "second_factor_locked") return "locked";
  if (code === "second_factor_unavailable") return "unavailable";
  if (status === 429) return "tooMany";
  if (status === 409) return "conflict";
  if (status === 401) return "expired";
  return "other";
}

/** Six ASCII digits, spaces allowed (as an app shows "123 456"); anything else isn't an app code. */
export function appCode(value: string): string | null {
  const digits = value.replace(/\s/g, "");
  return /^[0-9]{6}$/.test(digits) ? digits : null;
}

/** POSIX single quotes: always quoted, so no character of an email (', &, |, $, backtick) reaches the shell. */
export function shellQuote(value: string): string {
  return `'${value.replaceAll("'", "'\\''")}'`;
}

/** The CLI command that issues a setup ticket, up to (not including) the reason. */
export function enrolCommand(email: string): string {
  return `python -m app.admin enrol ${shellQuote(email)}`;
}
