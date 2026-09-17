// Plain logic for the accept-invite page, kept free of React so node --test can run it.

// What the invite email mints: <business id>.<secrets.token_urlsafe(32)>.
const TOKEN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.[A-Za-z0-9_-]{43}$/i;

/** Whether a link's fragment could be an invite at all; anything else was mangled on the way. */
export function isInviteToken(token: string): boolean {
  return TOKEN.test(token);
}

/** Which screen a lookup leads to: a form for a new or an existing account, the expired link, or a retry. */
export function inviteScreen(lookup: { status: number; data?: { has_account: boolean } }): "new" | "existing" | "expired" | "retry" {
  if (lookup.status === 200 && lookup.data) return lookup.data.has_account ? "existing" : "new";
  if (lookup.status === 400) return "expired";
  return "retry";
}

const PASSWORD_CODES = ["password_too_short", "password_too_long", "password_too_common"] as const;

export type Accepted =
  | "joined"
  | "expired"
  | "wrongPassword"
  | "accountExists"
  | "alreadyMember"
  | "tooMany"
  | (typeof PASSWORD_CODES)[number]
  | "other";

/** What an accept answer means for the page. */
export function acceptOutcome(answer: { status: number; code?: string }): Accepted {
  if (answer.status === 201) return "joined";
  if (answer.status === 400) return "expired";
  if (answer.status === 401) return "wrongPassword";
  if (answer.status === 429) return "tooMany";
  if (answer.status === 409 && answer.code === "account_exists") return "accountExists";
  if (answer.status === 409 && answer.code === "already_member") return "alreadyMember";
  if (answer.status === 422) return PASSWORD_CODES.find((code) => code === answer.code) ?? "other";
  return "other";
}

/** The accept request: the password goes exactly as typed, spaces and all, as sign-up and sign-in send it. */
export function acceptBody(token: string, password: string): { token: string; password: string } {
  return { token, password };
}
