import { headers } from "next/headers";
import { cache } from "react";

import type { BookingPageOut } from "@/api-client/types.gen";
import { slugLooksValid } from "@/lib/booking-page";
import { apiUrl, clientIp } from "@/lib/upstream";

type Loaded = { status: 200; data: BookingPageOut } | { status: 404 } | { status: "error" };

// cache(): one GET per request, shared by generateMetadata and the page itself (React's request
// dedup, not a cross-request cache — "no-store" below still forbids caching between requests).
export const load = cache(async (slug: string): Promise<Loaded> => {
  // Same shape the backend itself refuses (booking_page.py): never spend a request, or a rate-limit
  // hit, on a slug that could not possibly be stored.
  if (!slugLooksValid(slug)) return { status: 404 };
  const upstream = apiUrl();
  if (!upstream) return { status: "error" };
  // Set exactly as proxy.ts forwards it: the API's rate limiting and access logs see the visitor,
  // not this server.
  const ip = clientIp(await headers());
  try {
    const response = await fetch(new URL(`/api/public/booking-pages/${encodeURIComponent(slug)}`, upstream), {
      cache: "no-store",
      headers: ip ? { "x-forwarded-for": ip } : undefined,
    });
    if (response.status === 404) return { status: 404 };
    if (!response.ok) return { status: "error" };
    return { status: 200, data: (await response.json()) as BookingPageOut };
  } catch {
    return { status: "error" };
  }
});
