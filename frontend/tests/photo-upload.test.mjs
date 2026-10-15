// The service form's photo field (ZIF-133): the browser's checks before an upload, the server's
// refusal codes, and what Save sends.
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { checkFile, contentType, formatMb, MAX_BYTES, photoChange, savePhotoError, serverPhotoError } from "../lib/photo-upload.ts";

describe("checkFile", () => {
  // ZIF-133 F15. fence. Kills: an extension-only or MIME-only check, `<` for `<=` at the cap,
  // HEIC reported as a plain wrong type.
  const cases = [
    [{ type: "image/jpeg", name: "a.jpg", size: 1 }, null],
    [{ type: "image/png", name: "screenshot", size: MAX_BYTES }, null], // exactly 10 MiB is fine
    [{ type: "", name: "IMG_1.WEBP", size: 5 }, null], // no MIME: the extension decides
    [{ type: "image/jpeg", name: "big.jpg", size: MAX_BYTES + 1 }, { code: "tooBig", mb: 10.1 }],
    [{ type: "image/jpeg", name: "big.jpg", size: 14.2 * 1024 * 1024 }, { code: "tooBig", mb: 14.2 }],
    [{ type: "image/heic", name: "IMG_1.HEIC", size: 1 }, { code: "heic" }],
    [{ type: "", name: "IMG_1.heic", size: 1 }, { code: "heic" }],
    [{ type: "image/heif", name: "x", size: 1 }, { code: "heic" }],
    [{ type: "application/pdf", name: "photo.jpg", size: 1 }, { code: "type" }], // a renamed file
    [{ type: "image/gif", name: "a.gif", size: 1 }, { code: "type" }],
    [{ type: "", name: "notes.txt", size: 1 }, { code: "type" }],
    [{ type: "image/jpeg", name: "empty.jpg", size: 0 }, { code: "unreadable" }],
  ];
  for (const [file, expected] of cases) {
    test(`${JSON.stringify(file)} -> ${JSON.stringify(expected)}`, () => {
      assert.deepEqual(checkFile(file), expected);
    });
  }
});

test("the size reads in the reader's own number format, one decimal", () => {
  assert.equal(formatMb(14.2, "en"), "14.2");
  assert.equal(formatMb(14.2, "nl"), "14,2");
  assert.equal(formatMb(10, "pt"), "10,0");
});

test("the presign type falls back to the extension", () => {
  assert.equal(contentType({ type: "image/webp", name: "x.png" }), "image/webp");
  assert.equal(contentType({ type: "", name: "x.PNG" }), "image/png");
  assert.equal(contentType({ type: "", name: "x.webp" }), "image/webp");
  assert.equal(contentType({ type: "", name: "x.jpeg" }), "image/jpeg");
});

test("server codes map to the design's errors; anything else is a failed upload", () => {
  assert.equal(serverPhotoError("photo_type"), "type");
  assert.equal(serverPhotoError("photo_unreadable"), "unreadable");
  assert.equal(serverPhotoError("photo_too_large"), "tooBig");
  for (const code of ["photo_missing", "photos_unavailable", "busy", "rate_limited", undefined]) {
    assert.equal(serverPhotoError(code), "uploadFailed");
  }
});

describe("photoChange", () => {
  // ZIF-133 F16. fence. Kills: Save sending an upload still being checked, a removal sent as
  // "nothing", an untouched photo sent as a removal.
  test("blocked while uploading or checking", () => {
    assert.equal(photoChange({ kind: "uploading" }), "blocked");
    assert.equal(photoChange({ kind: "checking" }), "blocked");
  });
  test("a checked upload is sent; a removal is null; nothing else is sent", () => {
    assert.deepEqual(photoChange({ kind: "ready", uploadId: "u1", previous: "saved" }), { photo_upload_id: "u1" });
    assert.deepEqual(photoChange({ kind: "removed" }), { photo_upload_id: null });
    assert.deepEqual(photoChange({ kind: "saved" }), {});
  });
});

describe("savePhotoError", () => {
  // ZIF-133 F16. fence. Kills: a refusal about the photo shown as a banner (or the reverse), and
  // storage-down or rate-limited Saves not treated as a failed upload.
  const ready = { kind: "ready", uploadId: "u1", previous: "saved" };
  test("photo refusals land under the field", () => {
    assert.equal(savePhotoError(ready, "photo_unreadable"), "unreadable");
    assert.equal(savePhotoError(ready, "photo_type"), "type");
    assert.equal(savePhotoError(ready, "photo_missing"), "uploadFailed");
    assert.equal(savePhotoError(ready, "photo_too_large"), "uploadFailed");
    assert.equal(savePhotoError(ready, "photos_unavailable"), "uploadFailed");
    assert.equal(savePhotoError(ready, "rate_limited"), "uploadFailed");
  });
  test("anything else, or a Save without a new photo, stays a banner", () => {
    assert.equal(savePhotoError(ready, "busy"), null);
    assert.equal(savePhotoError(ready, "unknown_member"), null);
    assert.equal(savePhotoError(ready, undefined), null);
    assert.equal(savePhotoError({ kind: "removed" }, "photos_unavailable"), null);
  });
});
