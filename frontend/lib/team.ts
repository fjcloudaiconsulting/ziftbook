// Invite pure logic: who may invite, and the day count on a pending invite's row. Kept free of
// React so node --test can run it directly (the `lib/*.ts` rule, PRs 1-4).

/** Only an owner may invite, resend or revoke: the API enforces this (CurrentOwner, invites.py),
 * this only decides what the UI offers. */
export function canInvite(role: "owner" | "worker"): boolean {
  return role === "owner";
}

/** Whole days until `expiresAt`, rounded up, never negative. */
export function expiresIn(expiresAt: string, now: Date): number {
  const ms = new Date(expiresAt).getTime() - now.getTime();
  return Math.max(0, Math.ceil(ms / 86_400_000));
}

type Invite = { email: string; expires_at: string; expired: boolean };

/** The row reads "Invite expired" once there are zero days left, even when the server's own
 * `expired` flag hasn't caught up (a client clock running fast draws a distinction nobody can act
 * on: "expires in 0 days" reads as a bug, not information). */
export function isRowExpired(invite: Invite, now: Date): boolean {
  return invite.expired || expiresIn(invite.expires_at, now) === 0;
}

/** Replace the row with the same email (compared case-insensitively, as the server lowercases
 * it), keeping its position; append when the email is new. Both "invite someone" and "Send again"
 * go through this: a resend always gets a fresh id (`invites.py`'s `ON CONFLICT ... SET id =
 * excluded.id`), so matching the old row by id would leave it behind as a second, dead row whose
 * own link no longer resolves (Cancel on it 404s; the old row's Send again would look like it
 * works but silently rewrites yet another id). */
export function upsertInvite<T extends Invite>(list: T[], invite: T): T[] {
  const key = invite.email.toLowerCase();
  const index = list.findIndex((row) => row.email.toLowerCase() === key);
  if (index === -1) return [...list, invite];
  const next = list.slice();
  next[index] = invite;
  return next;
}

/** "Change role" asks straight for the one role the person doesn't have: with two roles there is
 * nothing to pick. */
export function otherRole(role: "owner" | "worker"): "owner" | "worker" {
  return role === "owner" ? "worker" : "owner";
}

/** The refusals the role ask names itself; null for the rest, which read as the generic problem
 * plus "Nothing was saved". The keep_an_owner trigger's 409 can't really reach this ask (you stay
 * an owner, and your own role has no button), but it keeps its own message all the same. */
export function roleChangeError(outcome: { status: number; code?: string }): "signedOut" | "notFound" | "lastOwner" | null {
  if (outcome.status === 401) return "signedOut";
  if (outcome.status === 404) return "notFound";
  if (outcome.status === 409 && outcome.code === "last_owner") return "lastOwner";
  return null;
}
