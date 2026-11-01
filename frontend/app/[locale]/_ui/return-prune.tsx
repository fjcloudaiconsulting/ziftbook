"use client";

import { useEffect } from "react";

import { forgetStaleReturn } from "@/lib/client-account";

/** ZIF-146: on every page load, the done screen's way back to a booking page goes once it is past its 24 hours
 * (lib/client-account.ts). Renders nothing. */
export function ReturnPrune() {
  useEffect(() => forgetStaleReturn(() => window.localStorage, Date.now()), []);
  return null;
}
