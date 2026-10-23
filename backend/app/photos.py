"""Service photos (ZIF-133): the browser PUTs the original to the uploads bucket on a presigned URL,
the API checks it (type from the bytes, size, pixels), re-encodes two WebP variants without
metadata and links them to a service when the owner saves it (services.py)."""

import io
import logging
import re
import struct
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from opentelemetry import trace
from opentelemetry.trace import SpanKind
from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from app import auth, limits, storage, tracing
from app.auth import CurrentOwner, SignedIn
from app.config import StorageSettings
from app.db import tenant_context
from app.errors import ApiError, Error
from app.jobs import Job

MAX_BYTES = 10 * 1024 * 1024
TYPES = ("image/jpeg", "image/png", "image/webp")
JPEG_CAP = 100_000_000
# Header pixels. Progressive JPEGs and every other format decode at full size (no draft shortcut):
# 8.3 MP is a 4K screenshot, and at 4 bytes a pixel it keeps a decode inside the 256Mi pod.
OTHER_CAP = 8_300_000
# libwebp keeps ~4 full-size buffers while decoding (8.3 MP lossy RGBA measured +176 MB).
WEBP_CAP = 4_000_000
Image.MAX_IMAGE_PIXELS = JPEG_CAP  # backstop only: the caps below refuse first
# One decode at a time (held from the read of the upload): the API pod is small.
_LOCK = threading.Lock()
LOCK_WAIT = 15  # seconds, then 503 busy
SRGB = ImageCms.createProfile("sRGB")
UUID_RE = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["photos"])


class PhotoUploadIn(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    content_type: str
    size: Annotated[int, Field(ge=1)]


class PhotoUploadOut(BaseModel):
    upload_id: UUID
    url: str
    headers: dict[str, str]


class PhotoChecked(BaseModel):
    width: int
    height: int


class Photo(BaseModel):
    thumb_url: str
    detail_url: str
    width: int
    height: int


@dataclass(frozen=True)
class Variants:
    thumb: bytes
    detail: bytes
    width: int
    height: int


def urls(key: str | None, width: int | None, height: int | None) -> Photo | None:
    """The photo's public URLs; None for no photo, or while ZIF_MEDIA_URL is unset."""
    base = StorageSettings().media_url.rstrip("/")
    if key is None or width is None or height is None or not base:
        return None
    return Photo(
        thumb_url=f"{base}/{key}/thumb.webp",
        detail_url=f"{base}/{key}/detail.webp",
        width=width,
        height=height,
    )


def guard(tenant_id: UUID, request: Request) -> None:
    """503 while storage is unset, 429 past 100 photo requests an hour for the business or for the
    address (a presign, a check or a Save with a photo each count)."""
    if not StorageSettings().configured():
        raise ApiError(503, "photos_unavailable")
    ip = request.client.host if request.client else None
    if limits.hit({f"photo:{tenant_id}": 100, limits.ip_key("photo", ip): 100}, timedelta(hours=1)):
        raise ApiError(429, "rate_limited")


def owner_tenant(request: Request) -> UUID:
    """The signed-in owner's business, resolved in its own short transaction (as signed_in does) so
    the check holds no pooled connection while it processes."""
    row = auth.resolve(request)
    with tenant_context(row.tenant_id) as db:
        auth.owner(auth.named(SignedIn(row.user_id, row.tenant_id, row.role, db)))
    tenant_id: UUID = row.tenant_id
    return tenant_id


@router.post(
    "/photo-uploads",
    name="presign",
    status_code=201,
    responses={s: {"model": Error} for s in (401, 403, 415, 422, 429, 503)},
)
def presign(
    body: PhotoUploadIn, current: CurrentOwner, request: Request, response: Response
) -> PhotoUploadOut:
    """A 5-minute URL for the browser to PUT one photo to; nothing is checked until it has."""
    guard(current.tenant_id, request)
    if body.content_type not in TYPES:
        raise ApiError(422, "photo_type")
    if body.size > MAX_BYTES:
        raise ApiError(422, "photo_too_large")
    upload_id = uuid.uuid4()
    url, headers = storage.presign_put(
        f"{current.tenant_id}/{upload_id}", body.content_type, body.size
    )
    response.headers["Cache-Control"] = "no-store"
    return PhotoUploadOut(upload_id=upload_id, url=url, headers=headers)


@router.post(
    "/photo-uploads/{upload_id}/check",
    name="check",
    responses={s: {"model": Error} for s in (401, 403, 409, 415, 422, 429, 503)},
)
def check(
    upload_id: UUID,
    tenant_id: Annotated[UUID, Depends(owner_tenant)],
    request: Request,
    response: Response,
) -> PhotoChecked:
    """What saving this upload would do, writing nothing: the errors before Save, and the size."""
    guard(tenant_id, request)
    found = process(tenant_id, upload_id)
    response.headers["Cache-Control"] = "no-store"
    return PhotoChecked(width=found.width, height=found.height)


def _unavailable(error: storage.StorageError) -> ApiError:
    logger.warning("photo storage failed", extra={"error": str(error)})
    return ApiError(503, "photos_unavailable")


def process(tenant_id: UUID, upload_id: UUID) -> Variants:
    """The two variants of an upload, or a 409/422/503 ApiError. The key is rebuilt from the
    session's business, never taken from the client."""
    refused: ApiError | None = None
    with tracing.span("service.photo.process", SpanKind.INTERNAL, {}) as span:
        try:
            # Held from the HEAD to the end of the decode, so a waiter never holds a 10 MiB body.
            if not _LOCK.acquire(timeout=LOCK_WAIT):
                raise ApiError(503, "busy")
            try:
                key = f"{tenant_id}/{upload_id}"
                n = storage.size(key)
                if n is None:
                    raise ApiError(409, "photo_missing")
                span.set_attribute("bytes", n)
                if n > MAX_BYTES:
                    raise ApiError(422, "photo_too_large")
                data = storage.read(key)
                if data is None:
                    raise ApiError(409, "photo_missing")
                found = variants(data)
            finally:
                _LOCK.release()
            span.set_attribute("outcome", "ok")
            return found
        except storage.StorageError as error:
            refused = _unavailable(error)
        except ApiError as error:
            # A refusal is the request's answer, not a failed span.
            refused = error
        span.set_attribute("outcome", refused.code)
    logger.info("photo refused", extra={"code": refused.code})
    raise refused


def _webp(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "WEBP", quality=80)  # no exif, icc or xmp passed: none is written
    return buf.getvalue()


def _srgb(im: Image.Image, icc: bytes | None) -> Image.Image:
    """The image in sRGB when it carried a profile of another RGB space (Display P3, say), so its
    colours survive the profile being dropped. An unusable profile leaves the pixels as they are."""
    if icc is None:
        return im
    try:
        return ImageCms.profileToProfile(im, ImageCms.ImageCmsProfile(io.BytesIO(icc)), SRGB) or im
    except ImageCms.PyCMSError, OSError:  # a corrupt profile keeps the pixels as they are
        return im


def _markers(data: bytes) -> Iterator[tuple[int, int]]:
    """A JPEG's markers after SOI with their offsets, up to its first scan (SOS) or EOI. Segments
    are skipped by length, so an EXIF thumbnail inside APP1 is never seen. The walk stops where a
    marker should start and none does, or at the end of the data."""
    i = 2
    while i + 1 < len(data) and data[i] == 0xFF:
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        yield i, marker
        if marker in (0xD9, 0xDA):  # EOI, SOS
            return
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:  # no length
            i += 2
        else:
            i += 2 + int.from_bytes(data[i + 2 : i + 4])


def multi_scan(data: bytes) -> bool:
    """Whether a JPEG codes its image in more than one scan, read from the markers (_markers).
    libjpeg buffers the whole image's coefficients for such a file, interleaved or not. Anything
    unreadable counts as yes."""
    frame = None
    try:
        for i, marker in _markers(data):
            if marker == 0xDA:  # SOS: Ns components in this first scan
                return frame is None or data[i + 4] < frame
            if marker in range(0xC0, 0xD0) and marker not in (0xC4, 0xC8, 0xCC) and frame is None:
                frame = data[i + 9]  # Nf
    except IndexError:
        pass
    return True


# SOF3, SOF7, SOF11, SOF15 (Huffman or arithmetic coded): libjpeg decodes a lossless frame at full
# width whatever draft() asked for.
LOSSLESS_FRAMES = frozenset((0xC3, 0xC7, 0xCB, 0xCF))
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
EXIF_CAP = 65_536  # bytes of EXIF: parsing it costs more than linear time


def unsafe_container(data: bytes) -> bool:
    """Whether an upload must not reach the decoder (ZB-33): a lossless JPEG, an animated PNG, or
    EXIF over 64 KiB in a JPEG's APP1 segments or a PNG's eXIf chunk. Read from the structure
    before Image.open (which already parses a JPEG's EXIF, for its dpi); never raises. A JPEG is
    also unsafe when its markers do not walk to the first scan, or pass an FF00 (no marker) on the
    way: a decoder skips such bytes and reads on, so the walk cannot vouch for the frame. A PNG is
    read up to IEND, as Pillow reads it (an eXIf after the image data is parsed too). Anything else
    is False: Image.open refuses junk. variants() checks EXIF again once the image is loaded."""
    if data[:2] == b"\xff\xd8":
        last, exif = None, 0
        for i, marker in _markers(data):
            if marker == 0x00 or marker in LOSSLESS_FRAMES:
                return True
            # Pillow joins every APP1 EXIF segment before the scan into one.
            if marker == 0xE1 and data[i + 4 : i + 10] == b"Exif\x00\x00":
                exif += max(int.from_bytes(data[i + 2 : i + 4]) - 2, 0)
            last = marker
        return last != 0xDA or exif > EXIF_CAP
    if data[:8] == PNG_SIGNATURE:
        i = 8
        while i + 8 <= len(data):
            length, kind = int.from_bytes(data[i : i + 4]), data[i + 4 : i + 8]
            if kind == b"acTL" or (kind == b"eXIf" and length > EXIF_CAP):
                return True
            if kind == b"IEND":
                break
            i += 12 + length
    return False


def variants(data: bytes) -> Variants:
    """Decode with the lock held (process). Memory is the point: the caps run on the header, a JPEG
    is decoded at a reduced size, and the image is shrunk to 1200px before any conversion."""
    if unsafe_container(data):  # before Image.open: an animated PNG allocates its canvas there
        raise ApiError(422, "photo_unreadable")
    try:
        opened = Image.open(io.BytesIO(data), formats=["JPEG", "PNG", "WEBP"])
    except UnidentifiedImageError:
        raise ApiError(422, "photo_type") from None
    except Image.DecompressionBombError, OSError:
        raise ApiError(422, "photo_unreadable") from None
    im: Image.Image = opened
    trace.get_current_span().set_attributes(
        {"format": str(im.format), "pixels": im.width * im.height}
    )
    # A progressive or multi-scan JPEG has no draft shortcut that saves memory, so it gets the
    # common cap.
    baseline = (
        im.format == "JPEG"
        and not (im.info.get("progressive") or im.info.get("progression"))
        and not multi_scan(data)
    )
    cap = JPEG_CAP if baseline else WEBP_CAP if im.format == "WEBP" else OTHER_CAP
    if im.width * im.height > cap:
        raise ApiError(422, "photo_unreadable")
    try:
        if im.format == "JPEG":
            im.draft("RGB", (1200, 1200))
        if im.width * im.height > OTHER_CAP:  # what load() would decode, after draft
            raise ApiError(422, "photo_unreadable")
        icc = im.info.get("icc_profile")
        if im.mode in ("I", "I;16", "I;16L", "I;16B"):  # 16-bit grey: scaled to 8 bits, not clipped
            im = im.convert("I").point(lambda v: v / 257).convert("L")
        elif im.mode not in ("L", "RGB", "RGBA"):  # palette, bilevel, LA, CMYK...
            im = im.convert("RGBA" if "A" in im.getbands() or "transparency" in im.info else "RGB")
        im.thumbnail((1200, 1200))
        im.load()  # a PNG's chunks after the image data reach im.info only here
        # The EXIF exif_transpose parses: info["exif"] (any format) or, from PNG text, the hex
        # "Raw profile type exif".
        exif, raw = im.info.get("exif") or b"", im.info.get("Raw profile type exif") or ""
        if len(exif) > EXIF_CAP or len(raw) > 2 * EXIF_CAP:
            raise ApiError(422, "photo_unreadable")
        # Pillow 13 raises these reading or rewriting malformed EXIF: text where it wants bytes, a
        # value that does not fit its tag, no TIFF header, a raw profile that is no hex.
        try:
            ImageOps.exif_transpose(im, in_place=True)
        except TypeError, AttributeError, struct.error, SyntaxError, ValueError:
            raise ApiError(422, "photo_unreadable") from None
        rgb = _srgb(im.convert("RGBA" if im.mode == "RGBA" else "RGB"), icc)
        return Variants(_webp(ImageOps.fit(rgb, (320, 320))), _webp(rgb), rgb.width, rgb.height)
    except OSError, ValueError, SyntaxError, Image.DecompressionBombError:
        raise ApiError(422, "photo_unreadable") from None


def finalize(tenant_id: UUID, service_id: UUID, upload_id: UUID) -> Variants:
    """Check the upload and write both variants under the service's key. Called with no row lock
    held; if the request then fails the variants are orphans.

    shortcut: orphans are never swept (rare, a few tens of KB each); add a sweep if they pile up.
    """
    found = process(tenant_id, upload_id)
    base = f"{tenant_id}/{service_id}/{upload_id}"
    try:
        storage.put_media(f"{base}/thumb.webp", found.thumb)
        storage.put_media(f"{base}/detail.webp", found.detail)
    except storage.StorageError as error:
        raise _unavailable(error) from None
    return found


def media_delete(job: Job) -> None:
    """Delete every media object under payload["prefix"]. The prefix must be the job's business or
    one of its photos; anything else raises (so the job retries, then gives up) and deletes
    nothing. A photo a service still references is kept: an owner can Save an upload they removed
    within the delete's grace."""
    prefix = job.payload.get("prefix")
    if (
        job.tenant_id is None
        or not isinstance(prefix, str)
        or not re.fullmatch(f"{job.tenant_id}/({UUID_RE}/{UUID_RE}/)?", prefix)
    ):
        raise ValueError("media.delete refuses its prefix")
    with tenant_context(job.tenant_id) as db:
        if db.scalar(text("SELECT true FROM services WHERE photo_key || '/' = :p"), {"p": prefix}):
            logger.info("media delete skipped: photo still in use")
            return
    storage.delete_prefix(prefix)
