"""The photo decode (ZIF-133, ZB-33), run only in the decoder's child process (photo_child.py,
ZIF-230): the checks before Pillow opens an upload, the caps on what it decodes, and the two WebP
variants. Imports Pillow and the standard library, never the app: the child holds no settings,
secrets or connections. photos.py reads the record this writes; the tests call decode() directly."""

import io
import json
import resource
import struct
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError

JPEG_CAP = 100_000_000
# Header pixels. Progressive JPEGs and every other format decode at full size (no draft shortcut):
# 8.3 MP is a 4K screenshot, and at 4 bytes a pixel it keeps a decode inside the child's memory.
OTHER_CAP = 8_300_000
# libwebp keeps ~4 full-size buffers while decoding (8.3 MP lossy RGBA measured +176 MB).
WEBP_CAP = 4_000_000
Image.MAX_IMAGE_PIXELS = JPEG_CAP  # backstop only: the caps below refuse first
SRGB = ImageCms.createProfile("sRGB")


class Refused(Exception):
    """The upload is refused with code (photo_type or photo_unreadable). error names why: the
    exception's class, or the check that refused it. format and pixels once the header was read."""

    def __init__(self, code: str, error: str) -> None:
        super().__init__(code)
        self.code, self.error = code, error
        self.format: str | None = None
        self.pixels: int | None = None


@dataclass(frozen=True)
class Decoded:
    thumb: bytes
    detail: bytes
    width: int
    height: int
    format: str
    pixels: int


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
# Bytes of colour profile (ZIF-230): Pillow's own ceiling for a PNG's inflated iCCP, for every
# format. Camera and phone profiles are a few KB.
ICC_CAP = 1024 * 1024
ICC_HEADER = b"ICC_PROFILE\x00"  # then a sequence number and a count, then the profile's bytes


def unsafe_container(data: bytes) -> bool:
    """Whether an upload must not reach the decoder (ZB-33): a lossless JPEG, an animated PNG,
    EXIF over 64 KiB in a JPEG's APP1 segments or a PNG's eXIf chunk, or a colour profile over
    ICC_CAP in a JPEG's APP2 segments, a PNG's iCCP chunk (compressed) or a WebP's ICCP chunk.
    Read from the structure before Image.open (which already parses a JPEG's EXIF, for its dpi, and
    joins its profile); never raises. A JPEG is also unsafe when its markers do not walk to the
    first scan, or pass an FF00 (no marker) on the way: a decoder skips such bytes and reads on, so
    the walk cannot vouch for the frame. A PNG is read up to IEND, as Pillow reads it (an eXIf
    after the image data is parsed too). Anything else is False: Image.open refuses junk.
    variants() checks EXIF and the profile again once the image is open."""
    if data[:2] == b"\xff\xd8":
        last, exif, icc = None, 0, 0
        for i, marker in _markers(data):
            if marker == 0x00 or marker in LOSSLESS_FRAMES:
                return True
            # Pillow joins every APP1 EXIF segment before the scan into one.
            if marker == 0xE1 and data[i + 4 : i + 10] == b"Exif\x00\x00":
                exif += max(int.from_bytes(data[i + 2 : i + 4]) - 2, 0)
            # And every APP2 profile segment (it reads them up to the frame; counting to the scan
            # is a superset).
            if marker == 0xE2 and data[i + 4 : i + 16] == ICC_HEADER:
                icc += max(int.from_bytes(data[i + 2 : i + 4]) - 2 - len(ICC_HEADER) - 2, 0)
            last = marker
        return last != 0xDA or exif > EXIF_CAP or icc > ICC_CAP
    if data[:8] == PNG_SIGNATURE:
        i = 8
        while i + 8 <= len(data):
            length, kind = int.from_bytes(data[i : i + 4]), data[i + 4 : i + 8]
            if kind == b"acTL" or (kind == b"eXIf" and length > EXIF_CAP):
                return True
            if kind == b"iCCP" and length > ICC_CAP:
                return True
            if kind == b"IEND":
                break
            i += 12 + length
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        i = 12
        while i + 8 <= len(data):
            kind, length = data[i : i + 4], int.from_bytes(data[i + 4 : i + 8], "little")
            if kind == b"ICCP" and length > ICC_CAP:
                return True
            i += 8 + length + (length & 1)  # a chunk's payload is padded to an even length
    return False


def decode(data: bytes) -> Decoded:
    """The two variants of an upload, or Refused. Memory is the point: the caps run on the header,
    a JPEG is decoded at a reduced size, and the image is shrunk to 1200px before any conversion."""
    seen: dict[str, Any] = {}
    try:
        return _decode(data, seen)
    except Refused as refused:
        refused.format, refused.pixels = seen.get("format"), seen.get("pixels")
        raise


def _decode(data: bytes, seen: dict[str, Any]) -> Decoded:
    if unsafe_container(data):  # before Image.open: an animated PNG allocates its canvas there
        raise Refused("photo_unreadable", "unsafe_container")
    try:
        opened = Image.open(io.BytesIO(data), formats=["JPEG", "PNG", "WEBP"])
    except UnidentifiedImageError as error:
        raise Refused("photo_type", type(error).__name__) from None
    # ValueError: Pillow refuses a profile or text chunk that inflates past 1 MiB while it opens.
    except (Image.DecompressionBombError, OSError, ValueError) as error:
        raise Refused("photo_unreadable", type(error).__name__) from None
    im: Image.Image = opened
    seen["format"], seen["pixels"] = im.format, im.width * im.height
    # A progressive or multi-scan JPEG has no draft shortcut that saves memory, so it gets the
    # common cap.
    baseline = (
        im.format == "JPEG"
        and not (im.info.get("progressive") or im.info.get("progression"))
        and not multi_scan(data)
    )
    cap = JPEG_CAP if baseline else WEBP_CAP if im.format == "WEBP" else OTHER_CAP
    if im.width * im.height > cap:
        raise Refused("photo_unreadable", "pixel_cap")
    try:
        if im.format == "JPEG":
            im.draft("RGB", (1200, 1200))
        if im.width * im.height > OTHER_CAP:  # what load() would decode, after draft
            raise Refused("photo_unreadable", "pixel_cap")
        icc = im.info.get("icc_profile")
        if icc is not None and len(icc) > ICC_CAP:  # before LittleCMS parses it (_srgb)
            raise Refused("photo_unreadable", "icc_cap")
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
            raise Refused("photo_unreadable", "exif_cap")
        # Pillow 13 raises these reading or rewriting malformed EXIF: text where it wants bytes, a
        # value that does not fit its tag, no TIFF header, a raw profile that is no hex.
        try:
            ImageOps.exif_transpose(im, in_place=True)
        except (TypeError, AttributeError, struct.error, SyntaxError, ValueError) as error:
            raise Refused("photo_unreadable", type(error).__name__) from None
        rgb = _srgb(im.convert("RGBA" if im.mode == "RGBA" else "RGB"), icc)
        thumb, detail = _webp(ImageOps.fit(rgb, (320, 320))), _webp(rgb)
        return Decoded(thumb, detail, rgb.width, rgb.height, str(seen["format"]), seen["pixels"])
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as error:
        raise Refused("photo_unreadable", type(error).__name__) from None


def usage() -> dict[str, int]:
    """This process's peak memory and CPU time so far, for the record: the peak is VmPeak (what
    RLIMIT_AS bounds) on Linux, the resident peak elsewhere."""
    used = resource.getrusage(resource.RUSAGE_SELF)
    peak_kb = used.ru_maxrss // 1024  # macOS reports bytes
    if sys.platform == "linux":
        with open("/proc/self/status") as status:
            peak_kb = next(int(line.split()[1]) for line in status if line.startswith("VmPeak:"))
    return {"peak_kb": peak_kb, "cpu_ms": round((used.ru_utime + used.ru_stime) * 1000)}


def record(data: bytes) -> bytes:
    """What the child writes after its ready marker: a 4-byte header length, a JSON header, then
    the thumb's and the detail's bytes (photos.py parses it, trusting none of it)."""
    head: dict[str, Any]
    try:
        found = decode(data)
    except Refused as refused:
        head, blobs = {"kind": "refused", "code": refused.code, "error": refused.error}, b""
        head |= {"format": refused.format, "pixels": refused.pixels}
    else:
        head = {"kind": "ok", "format": found.format, "pixels": found.pixels}
        head |= {"width": found.width, "height": found.height}
        head |= {"thumb": len(found.thumb), "detail": len(found.detail)}
        blobs = found.thumb + found.detail
    text = json.dumps(head | usage()).encode()
    return struct.pack(">I", len(text)) + text + blobs
