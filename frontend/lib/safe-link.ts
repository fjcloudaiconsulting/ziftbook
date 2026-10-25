// ZIF-146: the one way a booking, manage or account page may put text from the API into an href. Those
// pages share an origin with GET /api/account, so a link a merchant controls must never run script
// (javascript:), carry data (data:) or open anything but a web page or a mail to a plain address.
// tests/booking-page-safety.test.mjs fails for any other href or src built from API text on them.

// One address, nothing after it: a `?` or `&` would add headers or a body to the mail.
const MAIL = /^mailto:[^\s?&#,;:<>"'()[\]\\@]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$/;

/** The URL if it is an https: link or a mailto: of one plain address, else undefined (render no link). */
export function safeLink(value: string | null | undefined): string | undefined {
  if (!value) return undefined;
  if (MAIL.test(value)) return value;
  try {
    return new URL(value).protocol === "https:" ? value : undefined;
  } catch {
    return undefined;
  }
}
