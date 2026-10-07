// Plain logic for pages opened from an emailed link, kept free of React so node --test can run it.

/** The link the page shows: its token, and which opening of a link (a hashchange) brought it. */
export type OpenedLink = { token: string; opened: number };

/**
 * What to show once the fragment has been read. Every opening of a link starts over, even with the same token
 * (the email clicked again after the invite ended); reading the same opening twice keeps the same object, as
 * React's useSyncExternalStore needs.
 */
export function openedLink(shown: OpenedLink | null, fragment: string | null, opened: number): OpenedLink | null {
  if (fragment === null) return shown;
  if (shown?.token === fragment && shown.opened === opened) return shown;
  return { token: fragment, opened };
}

/**
 * One page's view of the link in the address bar, for useSyncExternalStore: every hashchange is a new opening
 * (only a new link fires it: taking the token out uses replaceState, which doesn't), and forget() ends the
 * link that was used so coming back to the page doesn't offer it again.
 */
export function linkStore(
  take: () => string | null,
  target: Pick<EventTarget, "addEventListener" | "removeEventListener">,
) {
  let shown: OpenedLink | null = null;
  let opened = 0;
  return {
    subscribe(changed: () => void) {
      const opening = () => {
        opened += 1;
        changed();
      };
      target.addEventListener("hashchange", opening);
      return () => target.removeEventListener("hashchange", opening);
    },
    snapshot: () => (shown = openedLink(shown, take(), opened)),
    forget() {
      shown = null;
    },
  };
}
