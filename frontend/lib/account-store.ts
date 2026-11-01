// ZIF-146: which client account this page has signed in or filled in from, for the header's account button. In
// memory only, for this page's life: never storage, never rendered on the server, and set only by the person's own
// action (signing in, the checkout's offer) or by an account page reading its own account.

type Account = { email: string; name: string | null };

export function accountStore<A extends Account = Account>() {
  let current: A | null = null;
  const changed = new Set<() => void>();
  const signedOut = new Set<() => void>();
  return {
    get: () => current,
    subscribe(listener: () => void) {
      changed.add(listener);
      return () => void changed.delete(listener);
    },
    show(account: A | null) {
      current = account;
      changed.forEach((listener) => listener());
    },
    /** A page that filled something in from the account empties it again when the account signs out. */
    onSignedOut(listener: () => void) {
      signedOut.add(listener);
      return () => void signedOut.delete(listener);
    },
    /** The account's session ended elsewhere (another tab, a reset): the button goes; no page empties what the
     * person typed, as it is still theirs. */
    ended() {
      this.show(null);
    },
    /** The account signed out on this browser: the button goes, and every page that listens is told. */
    signedOut() {
      this.show(null);
      signedOut.forEach((listener) => listener());
    },
  };
}
