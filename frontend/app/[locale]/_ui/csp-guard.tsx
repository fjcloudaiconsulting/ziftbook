import { headers } from "next/headers";

import { CSP_ENFORCE } from "@/lib/csp";

import { CspReload } from "./csp-reload";

/** On every personal page (their layouts render it): reloads the page if a client-side navigation brought it into
 * a document whose policy only reports (security code review N-9). The proxy put the enforced policy on this
 * request when this page enforces it. */
export async function CspGuard() {
  const enforced = (await headers()).has("content-security-policy");
  return <CspReload enforced={enforced} everywhere={CSP_ENFORCE} />;
}
