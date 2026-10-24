"use client";

import { createContext, type ReactNode, useContext } from "react";

const NonceContext = createContext<string | undefined>(undefined);

/** The page's script nonce (ZB-12), read once by the layout from the proxy's x-nonce. */
export function NonceProvider({ nonce, children }: { nonce: string | undefined; children: ReactNode }) {
  return <NonceContext value={nonce}>{children}</NonceContext>;
}

/** The nonce for a next/script: Next stamps its own scripts, but a <Script> it loads later needs it passed. */
export function useNonce(): string | undefined {
  return useContext(NonceContext);
}
