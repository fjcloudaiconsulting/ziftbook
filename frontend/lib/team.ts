// Invite pure logic: who may invite, and the day count on a pending invite's row. Kept free of
// React so node --test can run it directly (the `lib/*.ts` rule, PRs 1-4).

/** Only an owner may invite, resend or revoke: the API enforces this (CurrentOwner, invites.py),
 * this only decides what the UI offers. */
export function canInvite(role: "owner" | "worker"): boolean {
  return role === "owner";
}

/** Whole days until `expiresAt`, rounded up, never negative. The row shows "Invite expired" from
 * the server's own `InviteOut.expired` flag (clock skew safe); this is only the day count for the
 * "Invite expires in {n} days" case. */
export function expiresIn(expiresAt: string, now: Date): number {
  const ms = new Date(expiresAt).getTime() - now.getTime();
  return Math.max(0, Math.ceil(ms / 86_400_000));
}
