import { notFound } from "next/navigation";

// Addresses of two or more segments that no route owns ([slug] answers the one-segment ones), so they
// get the localized not-found page instead of Next's English one. Thrown from the metadata too: only
// then does the tab keep not-found's title once the page renders in the browser.
export async function generateMetadata() {
  notFound();
}

export default function UnknownAddress() {
  notFound();
}
