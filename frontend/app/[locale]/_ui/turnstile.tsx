"use client";

import Script from "next/script";
import { useLocale } from "next-intl";
import { useCallback, useEffect, useRef } from "react";

import { useNonce } from "./nonce";

type Api = {
  render(el: Element, options: Record<string, unknown>): string;
  remove(id: string): void;
};

// The app's pt is Brazilian Portuguese.
const LANGUAGE: Record<string, string> = { pt: "pt-br" };

/**
 * A visible Turnstile challenge. `onToken` gets the token when it is solved and null when it expires or
 * fails; a token is good for one request, so remount (change `key`) for another.
 */
export function Turnstile({ siteKey, action, onToken }: { siteKey: string; action: string; onToken: (token: string | null) => void }) {
  const locale = useLocale();
  const nonce = useNonce();
  const box = useRef<HTMLDivElement>(null);
  const widget = useRef<string | null>(null);
  const latest = useRef(onToken);
  useEffect(() => {
    latest.current = onToken;
  });

  const render = useCallback(() => {
    const api = (window as unknown as { turnstile?: Api }).turnstile;
    if (!api || !box.current || widget.current) return;
    widget.current = api.render(box.current, {
      sitekey: siteKey,
      action,
      "response-field": false,
      language: LANGUAGE[locale] ?? locale,
      callback: (token: string) => latest.current(token),
      "expired-callback": () => latest.current(null),
      "error-callback": () => latest.current(null),
    });
  }, [siteKey, action, locale]);

  // The script may already be there (a second mount): render now. Otherwise the script's onReady does.
  useEffect(() => {
    render();
    return () => {
      const api = (window as unknown as { turnstile?: Api }).turnstile;
      if (api && widget.current) api.remove(widget.current);
      widget.current = null;
    };
  }, [render]);

  return (
    <>
      <div ref={box} />
      <Script src="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit" strategy="afterInteractive" nonce={nonce} onReady={render} />
    </>
  );
}
