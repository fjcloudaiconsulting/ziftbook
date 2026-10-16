"use client";

import { useLocale, useTranslations } from "next-intl";
import { type ReactNode, type Ref, useEffect, useLayoutEffect, useRef, useState } from "react";

import { type Photo, photosCheck, photosPresign } from "@/api-client";
import { checkFile, contentType, type FileError, formatMb, serverPhotoError } from "@/lib/photo-upload";
import type { Placeholder } from "@/lib/placeholders";

import { JSON_WRITE, useConsole } from "../../_ui/console";
import { FieldError } from "../../_ui/parts";
import { ServiceTile } from "../../_ui/service-tile";
import styles from "../../_ui/ui.module.css";

// What the field holds (ZIF-133 design 1.3). `saved` is the service's stored photo, or none;
// nothing is written until the form's own Save (design decision 5a).
export type PhotoField =
  | { kind: "saved" }
  | { kind: "uploading"; percent: number; preview: string }
  | { kind: "checking"; preview: string }
  | { kind: "ready"; uploadId: string; preview: string; previous: "saved" | "removed" }
  | { kind: "removed" };

export type PhotoError = FileError | { code: "unreadable" | "uploadFailed" | "early" };

const ACCEPT = "image/jpeg,image/png,image/webp";

/** The browser's PUT straight to storage: XHR, not fetch, for the upload progress. */
function put(url: string, headers: Record<string, string>, file: File, onProgress: (percent: number) => void, holder: { current: XMLHttpRequest | null }) {
  return new Promise<"ok" | "failed" | "aborted">((resolve) => {
    const xhr = new XMLHttpRequest();
    holder.current = xhr;
    xhr.open("PUT", url);
    for (const [name, value] of Object.entries(headers)) xhr.setRequestHeader(name, value);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(Math.round((event.loaded / event.total) * 100));
    };
    xhr.onload = () => resolve(xhr.status >= 200 && xhr.status < 300 ? "ok" : "failed");
    xhr.onerror = () => resolve("failed");
    xhr.onabort = () => resolve("aborted");
    xhr.send(file);
  });
}

export function PhotoFieldset({
  saved,
  placeholder,
  value,
  onChange,
  error,
  onError,
  errorRef,
  pills,
  markDirty,
  onSignedOut,
}: {
  saved: Photo | null;
  placeholder: Placeholder;
  value: PhotoField;
  onChange(next: PhotoField | ((current: PhotoField) => PhotoField)): void;
  error: PhotoError | null;
  onError(next: PhotoError | null): void;
  errorRef: Ref<HTMLDivElement>;
  pills: ReactNode;
  markDirty(): void;
  onSignedOut(): void;
}) {
  const t = useTranslations("Console.services.form");
  const form = useTranslations("Form");
  const locale = useLocale();
  const { call } = useConsole();
  const xhr = useRef<XMLHttpRequest | null>(null);
  const run = useRef(0); // the latest attempt; an older one's late answers are ignored
  const [lastFile, setLastFile] = useState<File | null>(null); // "Try again" resends it, no picker
  const before = useRef<PhotoField>({ kind: "saved" }); // what Stop or a failure puts back
  const previews = useRef<string[]>([]);
  const side = useRef<HTMLDivElement>(null);
  const inside = useRef(false); // focus was in the field's controls when they were swapped

  useEffect(
    () => () => {
      // Leaving the form mid-upload: no late PUT, check or state change.
      run.current++;
      xhr.current?.abort();
      previews.current.forEach((url) => URL.revokeObjectURL(url));
    },
    [],
  );

  // Every state swaps the field's buttons, which would drop keyboard focus on <body>: put it on the
  // first control of the new state instead, or on the side itself while there is none (checking).
  useLayoutEffect(() => {
    if (!inside.current || !side.current) return;
    if (document.activeElement && document.activeElement !== document.body) return;
    const control = side.current.querySelector<HTMLElement>("button, input");
    (control ?? side.current).focus({ preventScroll: true });
  }, [value.kind, error]);

  async function start(file: File) {
    markDirty();
    onError(null);
    xhr.current?.abort();
    const refused = checkFile(file);
    if (refused) {
      onError(refused);
      return;
    }
    const attempt = ++run.current;
    setLastFile(file);
    if (value.kind !== "uploading" && value.kind !== "checking") before.current = value;
    const was = before.current;
    const previous = was.kind === "ready" ? was.previous : was.kind === "removed" ? "removed" : "saved";
    const preview = URL.createObjectURL(file);
    previews.current.push(preview);
    onChange({ kind: "uploading", percent: 0, preview });

    const fail = (status?: number, code?: string) => {
      if (attempt !== run.current) return;
      onChange(before.current);
      if (status === 401) return onSignedOut();
      const mapped = serverPhotoError(code);
      onError(mapped === "tooBig" ? { code: "tooBig", mb: Math.ceil((file.size / (1024 * 1024)) * 10) / 10 } : { code: mapped });
    };
    const presigned = await call(() => photosPresign({ body: { content_type: contentType(file), size: file.size } }), { write: true });
    if (attempt !== run.current) return;
    if (presigned.status !== 201 || !presigned.data) return fail(presigned.status, presigned.code);
    const { upload_id: uploadId, url, headers } = presigned.data;
    const sent = await put(url, headers, file, (percent) => onChange((current) => (current.kind === "uploading" && current.preview === preview ? { ...current, percent } : current)), xhr);
    if (attempt !== run.current || sent === "aborted") return;
    if (sent === "failed") return fail();
    onChange({ kind: "checking", preview });
    const checked = await call(() => photosCheck({ ...JSON_WRITE, path: { upload_id: uploadId } }), { write: true });
    if (attempt !== run.current) return;
    if (checked.status !== 200) return fail(checked.status, checked.code);
    onError(null); // a Save-too-early message is answered now
    onChange({ kind: "ready", uploadId, preview, previous });
  }

  function stop() {
    run.current++;
    xhr.current?.abort();
    onError(null);
    onChange(before.current);
  }

  function remove() {
    markDirty();
    onError(null);
    // No photo at all: a saved one goes on Save (Undo brings it back); a new one just goes.
    onChange(saved ? { kind: "removed" } : { kind: "saved" });
  }

  const picker = (label: string) => (
    <label className={`${styles.button} ${styles.secondary} ${styles.small} ${styles.filePicker}`}>
      {label}
      <input
        className={styles.srOnly}
        type="file"
        accept={ACCEPT}
        onChange={(event) => {
          const file = event.target.files?.[0];
          event.target.value = ""; // the same file again still fires a change
          if (file) void start(file);
        }}
      />
    </label>
  );

  const busy = value.kind === "uploading" || value.kind === "checking";
  const preview = value.kind === "uploading" || value.kind === "checking" || value.kind === "ready" ? value.preview : value.kind === "saved" ? (saved?.thumb_url ?? null) : null;
  const hasPhoto = preview !== null;

  // One live region, for the status line only (design: it reads "Uploading…" once, then the line).
  let status: ReactNode = null;
  let controls: ReactNode;
  if (value.kind === "uploading") {
    status = (
      <>
        <b>{t("photoUploading")}</b> <span className={styles.photoPct} aria-hidden="true">{`${value.percent}%`}</span>
      </>
    );
    controls = (
      <>
        <progress className={styles.photoProgress} value={value.percent} max={100} aria-label={t("photoProgress")}>
          {`${value.percent}%`}
        </progress>
        <button className={styles.textButton} type="button" onClick={stop}>
          {t("photoStop")}
        </button>
      </>
    );
  } else if (value.kind === "checking") {
    status = (
      <>
        <span className={styles.spinDark} aria-hidden="true" />
        <b>{t("photoChecking")}</b>
      </>
    );
  } else if (error?.code === "uploadFailed" && lastFile) {
    controls = (
      <div className={styles.photoBtns}>
        <button className={`${styles.button} ${styles.secondary} ${styles.small}`} type="button" onClick={() => void start(lastFile)}>
          {form("tryAgain")}
        </button>
        {picker(t("photoChooseAnother"))}
      </div>
    );
  } else if (value.kind === "removed") {
    status = t("photoRemoved");
    controls = (
      <div className={styles.photoBtns}>
        <button
          className={styles.textButton}
          type="button"
          onClick={() => onChange({ kind: "saved" })}
        >
          {t("photoUndo")}
        </button>
        {picker(t("photoAdd"))}
      </div>
    );
  } else if (hasPhoto) {
    if (value.kind === "ready") status = t("photoReady");
    controls = (
      <div className={styles.photoBtns}>
        {picker(t("photoReplace"))}
        <button className={styles.textButton} type="button" onClick={remove}>
          {t("photoRemove")}
        </button>
      </div>
    );
  } else {
    controls = picker(t("photoAdd"));
  }

  let message: string | null = null;
  if (error) {
    if (error.code === "tooBig") message = t("photoTooBig", { size: formatMb(error.mb, locale) });
    else if (error.code === "uploadFailed") message = saved && value.kind === "saved" ? `${t("photoUploadFailed")} ${t("photoCurrentStays")}` : t("photoUploadFailed");
    else message = t(`photoError.${error.code}`);
  }

  return (
    <fieldset className={styles.photoField}>
      <legend className={styles.label}>
        {t("photoLegend")} <span className={styles.optional}>{t("descriptionOptional")}</span>
      </legend>
      <div className={styles.photoRow}>
        <ServiceTile placeholder={placeholder} photo={preview} className={`${styles.tile96}${busy ? ` ${styles.tileDim}` : ""}`} />
        <div
          className={styles.photoSide}
          ref={side}
          tabIndex={-1}
          onFocus={() => {
            inside.current = true;
          }}
          onBlur={(event) => {
            // Moved to another control on the page: the owner left. (A removed button blurs with no target.)
            if (event.relatedTarget && !side.current?.contains(event.relatedTarget)) inside.current = false;
          }}
        >
          <p className={styles.photoStatus} aria-live="polite">
            {status}
          </p>
          {controls}
        </div>
      </div>
      {message && (
        // role="alert": it arrives after a wait, not on submit. Focusable for Save-too-early (j).
        <div ref={errorRef} tabIndex={-1} role="alert">
          <FieldError id="service-photo-error">{message}</FieldError>
        </div>
      )}
      <p className={styles.hint}>{t("photoHint")}</p>
      {!hasPhoto && pills}
    </fieldset>
  );
}
