// Plain logic for the account screens, kept free of React so node --test can run it.

export const RESEND_AFTER_MS = 60_000;

/** Whole seconds until another link may be sent; 0 once it may. */
export function secondsLeft(sentAt: number, now: number): number {
  return Math.max(0, Math.ceil((sentAt + RESEND_AFTER_MS - now) / 1000));
}

export function clock(seconds: number): string {
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

type Place = {
  location: { hash: string; pathname: string; search: string };
  history: { replaceState(data: unknown, unused: string, url: string): void };
  sessionStorage: { getItem(key: string): string | null; setItem(key: string, value: string): void };
};

/**
 * The token from an emailed link's fragment. The fragment leaves the address bar first, so it can't end up in
 * history, a bookmark or a shared screenshot; sessionStorage keeps it for a reload of this tab.
 */
export function takeToken(place: Place, key: string): string | null {
  const token = place.location.hash.slice(1);
  if (token) {
    place.history.replaceState(null, "", place.location.pathname + place.location.search);
    try {
      place.sessionStorage.setItem(key, token);
    } catch {
      // Storage blocked: the token still works until the page is reloaded.
    }
    return token;
  }
  try {
    return place.sessionStorage.getItem(key);
  } catch {
    return null;
  }
}
