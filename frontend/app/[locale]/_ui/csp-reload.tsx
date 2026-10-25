"use client";

import { useEffect } from "react";

import { routing } from "@/i18n/routing";
import { mustReload } from "@/lib/personal-pages";

/** CspGuard's client half: the document's own load path decides, never the current URL. */
export function CspReload({ enforced, everywhere }: { enforced: boolean; everywhere: boolean }) {
  useEffect(() => {
    const [entry] = performance.getEntriesByType("navigation");
    const documentPath = entry ? new URL(entry.name).pathname : null;
    if (mustReload(enforced, everywhere, documentPath, routing.locales)) window.location.reload();
  }, [enforced, everywhere]);
  return null;
}
