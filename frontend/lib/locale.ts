// ZIF-102: which language a page opens in, as plain logic (CONTRIBUTING.md, "Which language a person
// sees"). It matches against routing.locales only, never a list of its own (ruling 5).

// A language the person picked with the language button in this tab that could not be saved to
// their account. It applies to this tab until the page is loaded again ("on this device only for
// now"); a saved pick needs nothing here, the account holds it.
let unsaved: string | null = null;
// Whether the page a pick led to still has to say it could not be saved (once).
let untold = false;

export function recordPick(locale: string, saved: boolean): void {
  unsaved = saved ? null : locale;
  untold = !saved;
}

/** True once, on the page in the language of a pick that could not be saved: the language menu
 * then opens there to say so. */
export function takeUnsavedNotice(locale: string): boolean {
  if (!untold || unsaved !== locale) return false;
  untold = false;
  return true;
}

export function pickedHere(): string | null {
  return unsaved;
}

/** On sign-out: the next person's own language applies. */
export function forgetPick(): void {
  unsaved = null;
  untold = false;
}
