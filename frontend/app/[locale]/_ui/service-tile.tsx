"use client";

import { type ReactNode, useState } from "react";

import type { Placeholder } from "@/lib/placeholders";

import styles from "./ui.module.css";

// The five placeholder glyphs (ZIF-133 design, batch-b): 24-unit grid, stroke only, round caps, drawn
// inline so a placeholder never needs a request.
const GLYPHS: Record<Placeholder, ReactNode> = {
  generic: (
    <>
      <rect x="3.5" y="5" width="17" height="15" rx="2.5" />
      <path d="M3.5 9.5h17M8 3.5v3M16 3.5v3" />
      <path d="M9 14.75l2 2 4-4" />
    </>
  ),
  nails: (
    <>
      <path d="M11 2.75h2l.6 6.5h-3.2z" />
      <path d="M8.5 9.25h7l1.5 2v7.25a2.75 2.75 0 0 1-2.75 2.75h-5.5A2.75 2.75 0 0 1 6 18.5v-7.25z" />
      <path d="M9 14v3.5" />
    </>
  ),
  hair: (
    <>
      <circle cx="7" cy="17.5" r="2.75" />
      <circle cx="17" cy="17.5" r="2.75" />
      <path d="M8.7 15.3L17.5 3.5M15.3 15.3L6.5 3.5" />
    </>
  ),
  beauty: (
    <>
      <path d="M8.5 21h7v-7.5h-7z" />
      <path d="M9.5 13.5V10h5v3.5" />
      <path d="M10.25 10V5.5l3.5-2.25V10" />
    </>
  ),
  massage: (
    <>
      <path d="M8.5 12V6a1.25 1.25 0 0 1 2.5 0v5" />
      <path d="M11 11V4.75a1.25 1.25 0 0 1 2.5 0V11" />
      <path d="M13.5 11V6a1.25 1.25 0 0 1 2.5 0v6.5" />
      <path d="M8.5 11.5V9a1.25 1.25 0 0 0-2.5 0v5.5c0 3.6 2.4 6 5.6 6h.4c2.9 0 4.4-1.8 5.1-3.8l1.5-4.2a1.3 1.3 0 0 0-2.4-1L16 13.5" />
    </>
  ),
};

/** A service's square picture: its photo, else its placeholder. Decorative either way (alt="",
 * aria-hidden): the name is always printed next to it. A photo that fails to load turns into the
 * placeholder; one still loading is the plain --surface tile. `className` sets the size. */
export function ServiceTile({ placeholder, photo, className }: { placeholder: Placeholder; photo?: string | null; className?: string }) {
  const [failed, setFailed] = useState<string | null>(null);
  if (photo && failed !== photo) {
    return (
      <span className={`${styles.tile}${className ? ` ${className}` : ""}`}>
        {/* The thumb variant is 320x320 (photos.py); the tile's CSS sets the shown size. */}
        {/* eslint-disable-next-line @next/next/no-img-element -- the CDN serves sized variants: no optimiser */}
        <img src={photo} width={320} height={320} alt="" loading="lazy" decoding="async" onError={() => setFailed(photo)} />
      </span>
    );
  }
  return (
    <span className={`${styles.tile} ${styles.tilePlaceholder}${className ? ` ${className}` : ""}`} aria-hidden="true">
      <svg viewBox="0 0 24 24">{GLYPHS[placeholder]}</svg>
    </span>
  );
}
