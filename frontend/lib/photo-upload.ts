// Pure logic behind the service form's photo field (ZIF-133): the browser's own checks before an
// upload, the server's refusal codes, and what Save sends. Free of React and sibling imports, as
// lib/services.ts, so node --test runs it directly.
export const MAX_BYTES = 10 * 1024 * 1024; // the server's cap (photos.py), checked again there

export type FileError = { code: "heic" } | { code: "type" } | { code: "unreadable" } | { code: "tooBig"; mb: number };

const TYPES = ["image/jpeg", "image/png", "image/webp"];
const EXTENSIONS = /\.(jpe?g|png|webp)$/i;

/** null when the file may be uploaded. HEIC first (it has its own message), then the type by MIME
 * or, when the browser gives none, by extension, then the size. */
export function checkFile(file: { type: string; name: string; size: number }): FileError | null {
  if (/^image\/hei[cf]/i.test(file.type) || /\.hei[cf]$/i.test(file.name)) return { code: "heic" };
  if (file.type ? !TYPES.includes(file.type) : !EXTENSIONS.test(file.name)) return { code: "type" };
  // Rounded up, so a file just over the cap never reads "10.0 MB" next to "10 MB or less".
  if (file.size > MAX_BYTES) return { code: "tooBig", mb: Math.ceil((file.size / (1024 * 1024)) * 10) / 10 };
  if (file.size === 0) return { code: "unreadable" }; // the presign refuses it; no Try again can help
  return null;
}

/** The type the presign asks for: the browser's, or from the extension when it gave none (the
 * server sniffs the bytes anyway). Only called on a file checkFile passed. */
export function contentType(file: { type: string; name: string }): string {
  if (file.type) return file.type;
  if (/\.png$/i.test(file.name)) return "image/png";
  if (/\.webp$/i.test(file.name)) return "image/webp";
  return "image/jpeg";
}

/** "14.2" / "14,2": one decimal, in the reader's own number format. */
export function formatMb(mb: number, locale: string): string {
  return new Intl.NumberFormat(locale, { minimumFractionDigits: 1, maximumFractionDigits: 1 }).format(mb);
}

export type ServerPhotoError = "type" | "unreadable" | "tooBig" | "uploadFailed";

/** The field error for a refused presign, check or Save. Anything not about the file itself
 * (expired upload, storage down, busy, network) is "didn't upload, try again". */
export function serverPhotoError(code: string | undefined): ServerPhotoError {
  if (code === "photo_type") return "type";
  if (code === "photo_unreadable") return "unreadable";
  if (code === "photo_too_large") return "tooBig";
  return "uploadFailed";
}

export type PhotoState =
  | { kind: "saved" } // the service's stored photo, or none: nothing to send
  | { kind: "uploading" | "checking" }
  // previous: what a Save refused over this photo goes back to (design 1.4).
  | { kind: "ready"; uploadId: string; previous: "saved" | "removed" }
  | { kind: "removed" }; // a stored photo the owner removed, not saved yet

/** What Save sends about the photo: "blocked" while an upload or check runs (design error j). */
export function photoChange(state: PhotoState): "blocked" | { photo_upload_id?: string | null } {
  if (state.kind === "uploading" || state.kind === "checking") return "blocked";
  if (state.kind === "ready") return { photo_upload_id: state.uploadId };
  if (state.kind === "removed") return { photo_upload_id: null };
  return {};
}

/** A Save refused over its new photo: the field error to show, or null when the refusal is about
 * something else (that one stays a banner). tooBig can't be the file's fault here (it was checked),
 * so it reads as a failed upload. */
export function savePhotoError(state: PhotoState, code: string | undefined): ServerPhotoError | null {
  if (state.kind !== "ready") return null;
  if (!(code?.startsWith("photo_") || code === "photos_unavailable" || code === "rate_limited")) return null;
  const mapped = serverPhotoError(code);
  return mapped === "tooBig" ? "uploadFailed" : mapped;
}
