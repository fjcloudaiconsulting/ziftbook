// The business's contact email as a link (ZIF-99), kept free of React so node --test can run it.

/** A mailto: link to exactly this one address. The address is text the owner typed: every character
 * that means something in a URL (?, &, #, %, spaces, line breaks) is percent-encoded on both sides of
 * the last @, so it can never add a header, a body or another recipient (RFC 6068). */
export function mailtoHref(email: string): string {
  const at = email.lastIndexOf("@");
  if (at === -1) return `mailto:${encodeURIComponent(email)}`;
  return `mailto:${encodeURIComponent(email.slice(0, at))}@${encodeURIComponent(email.slice(at + 1))}`;
}
