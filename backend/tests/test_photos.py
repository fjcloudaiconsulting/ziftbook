"""ZIF-133: storage under moto and the photo pipeline: presign, check, resize, media.delete."""

import io
import logging
import os
import struct
import subprocess
import sys
import uuid
import zlib
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import boto3
import PIL
import pytest
from botocore.exceptions import EndpointConnectionError
from mypy_boto3_s3 import S3Client
from PIL import Image, ImageCms, ImageFile, ImageOps
from sqlalchemy import text

from app import photos, storage, worker
from app.db import tenant_context
from app.errors import ApiError
from app.jobs import Job
from tests.conftest import API_DIR, People, image, upload
from tests.test_bookings_db import seed_service


def refusal(tenant: uuid.UUID, upload_id: uuid.UUID) -> tuple[int, str]:
    with pytest.raises(ApiError) as error:
        photos.process(tenant, upload_id)
    return error.value.status_code, error.value.code


# ZIF-133 F7. fence. Kills: the object key built from a client value; boto3's default CRC checksum
# left on (browser PUTs and S3-compatibles break on it); a signed header beyond content-length,
# content-type and host (R2's CORS allows only content-type).
def test_presign(s3: S3Client) -> None:
    tenant, upload_id = uuid.uuid4(), uuid.uuid4()
    url, headers = storage.presign_put(f"{tenant}/{upload_id}", "image/png", 1234)
    parsed, query = urlparse(url), parse_qs(urlparse(url).query)
    assert parsed.netloc == "localhost:8333"
    assert parsed.path == f"/ziftbook-uploads/{tenant}/{upload_id}"
    assert query["X-Amz-Expires"] == ["300"]
    assert query["X-Amz-SignedHeaders"] == ["content-length;content-type;host"]
    # botocore-stubs lacks the attribute; it exists at runtime.
    config = storage.client().meta.config
    assert config.request_checksum_calculation == "when_required"  # type: ignore[attr-defined]
    assert config.response_checksum_validation == "when_required"  # type: ignore[attr-defined]
    # Bounded waits: a stalled store must not pin a request or a job thread.
    assert (config.connect_timeout, config.read_timeout) == (5, 15)  # type: ignore[attr-defined]
    # max_attempts=2 counts the retries after the first try.
    assert config.retries["total_max_attempts"] == 3  # type: ignore[attr-defined]
    assert not [k for k in query if "checksum" in k.lower()]
    assert headers == {"Content-Type": "image/png"}


# ZIF-133 F7. fence. Kills: a server-side PUT that sends boto3's default CRC header.
def test_server_side_puts_carry_no_default_checksum(
    s3: S3Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []
    real = storage.client

    def spy(**kw: bool) -> S3Client:
        c = real(**kw)
        c.meta.events.register(
            "before-send.s3", lambda request, **_: sent.extend(request.headers.keys())
        )
        return c

    monkeypatch.setattr(storage, "client", spy)
    storage.put_media("t/x.webp", b"abc")
    assert sent
    assert not [h for h in sent if "checksum" in h.lower()]


# ZIF-133 G4. guard: with no public endpoint the presigned host is the S3 endpoint itself.
def test_presign_uses_the_endpoint_when_there_is_no_public_one(
    s3: S3Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ZIF_S3_PUBLIC_ENDPOINT")
    url, _ = storage.presign_put("t/x", "image/png", 1)
    assert urlparse(url).netloc == "s3.amazonaws.com"


# ZIF-133 F8. fence. Kills: the key looked up by upload id alone, not under the session's business.
# ZIF-133 F9. fence. Kills: a HEAD miss caught as "NoSuchKey" only (a real one is a bare "404").
def test_missing_and_cross_tenant(s3: S3Client) -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    upload_id = upload(s3, a, image("PNG", (10, 10)))
    assert refusal(b, upload_id) == (409, "photo_missing")
    assert refusal(a, uuid.uuid4()) == (409, "photo_missing")
    assert storage.size(f"{a}/{upload_id}") is not None


# ZIF-133 F10. fence. Kills: the 10 MiB cap checked only at presign (the declared size is the
# client's word; the HEAD of what arrived is the cap).
def test_too_large(s3: S3Client) -> None:
    t = uuid.uuid4()
    big = upload(s3, t, b"\0" * (photos.MAX_BYTES + 1))
    assert refusal(t, big) == (422, "photo_too_large")


# ZIF-133 F10. fence. Kills: trusting the declared content type; no format allowlist; a truncated
# file passed on.
def test_sniff(s3: S3Client) -> None:
    t = uuid.uuid4()
    # Declared as a JPEG upload, but the bytes are a PNG: accepted, the format comes from the bytes.
    png = upload(s3, t, image("PNG", (20, 10)), content_type="image/jpeg")
    assert photos.process(t, png).width == 20
    assert refusal(t, upload(s3, t, b"hello, not an image")) == (422, "photo_type")
    assert refusal(t, upload(s3, t, image("GIF", (10, 10)))) == (422, "photo_type")
    jpeg = image("JPEG", (400, 400))
    assert refusal(t, upload(s3, t, jpeg[: len(jpeg) // 2])) == (422, "photo_unreadable")


def no_load(monkeypatch: pytest.MonkeyPatch) -> None:
    def load(self: ImageFile.ImageFile) -> None:
        raise AssertionError("decoded")

    monkeypatch.setattr(ImageFile.ImageFile, "load", load)


# ZIF-133 F10. fence. Kills: the pixel cap checked after load() instead of from the header.
def test_png_over_cap_refused_before_decode(s3: S3Client, monkeypatch: pytest.MonkeyPatch) -> None:
    t = uuid.uuid4()
    buf = io.BytesIO()
    Image.new("1", (5000, 5000)).save(buf, "PNG")
    upload_id = upload(s3, t, buf.getvalue())
    no_load(monkeypatch)
    assert refusal(t, upload_id) == (422, "photo_unreadable")


# ZIF-133 F10. fence. Kills: the cap checked on the header size only (JPEG draft() scales by
# min(w//1200, h//1200), so 20000x900 decodes at full size).
def test_jpeg_over_cap_after_draft_refused_before_decode(
    s3: S3Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = uuid.uuid4()
    upload_id = upload(s3, t, image("JPEG", (20000, 900)))
    no_load(monkeypatch)
    assert refusal(t, upload_id) == (422, "photo_unreadable")


# ZIF-133 F11. fence. Kills: saving with exif=im.info["exif"]; the orientation not applied.
def test_exif_orientation_applied_then_dropped(s3: S3Client) -> None:
    t = uuid.uuid4()
    exif = Image.Exif()
    exif[0x0112] = 6
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2] = "N", (52.0, 22.0, 1.0)
    v = photos.process(t, upload(s3, t, image("JPEG", (400, 200), exif=exif.tobytes())))
    assert (v.width, v.height) == (200, 400)
    for blob in (v.thumb, v.detail):
        assert not dict(Image.open(io.BytesIO(blob)).getexif())
        assert b"Exif" not in blob and b"EXIF" not in blob


def profile(
    red: tuple[float, float, float],
    green: tuple[float, float, float],
    blue: tuple[float, float, float],
) -> bytes:
    """A minimal ICC v2 matrix/TRC RGB profile with gamma 2.2 (D50-adapted primaries)."""

    def fixed(*values: float) -> bytes:
        return b"".join(struct.pack(">i", round(v * 65536)) for v in values)

    tags = {
        b"wtpt": b"XYZ \0\0\0\0" + fixed(0.9642, 1.0, 0.8249),
        b"rXYZ": b"XYZ \0\0\0\0" + fixed(*red),
        b"gXYZ": b"XYZ \0\0\0\0" + fixed(*green),
        b"bXYZ": b"XYZ \0\0\0\0" + fixed(*blue),
        b"rTRC": b"curv\0\0\0\0" + struct.pack(">IH", 1, round(2.2 * 256)) + b"\0\0",
    }
    tags[b"gTRC"] = tags[b"bTRC"] = tags[b"rTRC"]
    offset = 128 + 4 + 12 * len(tags)
    table, body = b"", b""
    for sig, data in tags.items():
        table += sig + struct.pack(">II", offset + len(body), len(data))
        body += data
    header = struct.pack(">I4xI4s4s4s", offset + len(body), 0x02400000, b"mntr", b"RGB ", b"XYZ ")
    header += bytes(12) + b"acsp" + bytes(24) + struct.pack(">I", 0) + fixed(0.9642, 1.0, 0.8249)
    return header.ljust(128, b"\0") + struct.pack(">I", len(tags)) + table + body


# ZIF-133 F11. fence. Kills: dropping an embedded colour profile without converting to sRGB (a
# Display P3 photo then renders duller than it was taken).
def test_a_wide_gamut_profile_is_converted_to_srgb_before_it_is_dropped(s3: S3Client) -> None:
    p3 = profile((0.5151, 0.2412, -0.0011), (0.2920, 0.6922, 0.0419), (0.1571, 0.0666, 0.7841))
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), (200, 100, 50)).save(buf, "PNG", icc_profile=p3)
    t = uuid.uuid4()
    v = photos.process(t, upload(s3, t, buf.getvalue()))
    red, green, _ = Image.open(io.BytesIO(v.detail)).convert("RGB").getpixel((20, 20))  # type: ignore[misc]
    assert red > 215  # P3 (200, 100, 50) is a more saturated sRGB red than its numbers say
    assert green < 95
    assert "icc_profile" not in Image.open(io.BytesIO(v.detail)).info
    # sRGB stays itself: a plain image is untouched by the conversion.
    srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), (200, 100, 50)).save(buf, "PNG", icc_profile=srgb)
    v = photos.process(t, upload(s3, t, buf.getvalue()))
    red, green, _ = Image.open(io.BytesIO(v.detail)).convert("RGB").getpixel((20, 20))  # type: ignore[misc]
    assert abs(red - 200) < 8 and abs(green - 100) < 8


# ZIF-133 F12. fence. Kills: a Cache-Control without s-maxage; a detail variant scaled up.
@pytest.mark.parametrize(
    ("fmt", "size", "detail"),
    [("JPEG", (3000, 2000), (1200, 800)), ("PNG", (600, 400), (600, 400))],
)
def test_variants(s3: S3Client, fmt: str, size: tuple[int, int], detail: tuple[int, int]) -> None:
    t, service = uuid.uuid4(), uuid.uuid4()
    upload_id = upload(s3, t, image(fmt, size))
    v = photos.finalize(t, service, upload_id)
    assert (v.width, v.height) == detail
    for name, want in (("thumb", (320, 320)), ("detail", detail)):
        obj = s3.get_object(Bucket="ziftbook-media", Key=f"{t}/{service}/{upload_id}/{name}.webp")
        assert obj["ContentType"] == "image/webp"
        assert obj["CacheControl"] == "public, max-age=31536000, s-maxage=86400, immutable"
        assert Image.open(io.BytesIO(obj["Body"].read())).size == want


# ZIF-133 G1. guard: the decode runs on the draft-reduced JPEG, and the orientation still applies.
def test_draft_then_transpose(s3: S3Client) -> None:
    t = uuid.uuid4()
    exif = Image.Exif()
    exif[0x0112] = 6
    v = photos.process(t, upload(s3, t, image("JPEG", (5000, 3000), exif=exif.tobytes())))
    assert (v.width, v.height) == (720, 1200)


# ZIF-133 G1. guard: a busy decoder answers 503 instead of queueing without bound.
def test_a_busy_decoder_is_a_503(s3: S3Client, monkeypatch: pytest.MonkeyPatch) -> None:
    t = uuid.uuid4()
    upload_id = upload(s3, t, image("PNG", (10, 10)))
    monkeypatch.setattr(photos, "LOCK_WAIT", 0.01)
    read: list[str] = []
    monkeypatch.setattr(storage, "read", lambda key: read.append(key))
    with photos._LOCK:
        assert refusal(t, upload_id) == (503, "busy")
    assert read == []  # a waiter never holds the body it has not decoded yet


def media_count(s3: S3Client) -> int:
    pages = s3.get_paginator("list_objects_v2").paginate(Bucket="ziftbook-media")
    return sum(page["KeyCount"] for page in pages)


def delete_job(tenant: uuid.UUID | None, prefix: object) -> Job:
    return Job(uuid.uuid4(), "media.delete", tenant, {"prefix": prefix})


# ZIF-133 F14. fence. Kills: a handler without the prefix check (an empty prefix wipes the bucket),
# or one that deletes a photo a service still uses.
@pytest.mark.usefixtures("bound")
def test_media_delete(s3: S3Client) -> None:
    t, other = uuid.uuid4(), uuid.uuid4()
    s, u = uuid.uuid4(), uuid.uuid4()
    for i in range(1001):
        s3.put_object(Bucket="ziftbook-media", Key=f"{t}/{s}/{uuid.uuid4()}/{i}.webp", Body=b"x")
    keep = f"{other}/{s}/{u}/thumb.webp"
    s3.put_object(Bucket="ziftbook-media", Key=keep, Body=b"x")
    for bad in ("", "/", f"{other}/", f"{t}/{s}/", f"{other}/{s}/{u}/", str(t), None, 7):
        with pytest.raises(ValueError):
            photos.media_delete(delete_job(t, bad))
    with pytest.raises(ValueError):
        photos.media_delete(delete_job(None, "None/"))
    assert media_count(s3) == 1002  # nothing was deleted
    photos.media_delete(delete_job(t, f"{t}/"))
    assert media_count(s3) == 1
    assert [o["Key"] for o in s3.list_objects_v2(Bucket="ziftbook-media")["Contents"]] == [keep]


# ZIF-133 F14. fence. Kills: deleting the prefix of a photo that a service references again (an
# owner re-saved the upload id they had removed, inside the delete's grace).
def test_media_delete_keeps_a_photo_a_service_uses(s3: S3Client, people: People) -> None:
    service = seed_service(people.a)
    u = uuid.uuid4()
    prefix = f"{people.a}/{service}/{u}/"
    s3.put_object(Bucket="ziftbook-media", Key=f"{prefix}thumb.webp", Body=b"x")
    with tenant_context(people.a) as db:
        db.execute(
            text("""
            UPDATE services SET photo_key = :k, photo_content_type = 'image/webp',
              photo_width = 10, photo_height = 10 WHERE id = :s
            """),
            {"k": prefix.rstrip("/"), "s": service},
        )
    photos.media_delete(delete_job(people.a, prefix))
    assert s3.list_objects_v2(Bucket="ziftbook-media")["KeyCount"] == 1
    with tenant_context(people.a) as db:
        db.execute(
            text(
                "UPDATE services SET photo_key = NULL, photo_content_type = NULL, "
                "photo_width = NULL, photo_height = NULL WHERE id = :s"
            ),
            {"s": service},
        )
    photos.media_delete(delete_job(people.a, prefix))
    assert s3.list_objects_v2(Bucket="ziftbook-media")["KeyCount"] == 0


# ZIF-133 G3. guard: the URLs of a photo, whatever slashes ZIF_MEDIA_URL ends with.
def test_photo_urls(s3: S3Client, monkeypatch: pytest.MonkeyPatch) -> None:
    key = "t/s/u"
    found = photos.urls(key, 1200, 800)
    assert found is not None
    assert found.thumb_url == "https://img.example.test/t/s/u/thumb.webp"
    assert found.detail_url == "https://img.example.test/t/s/u/detail.webp"
    assert (found.width, found.height) == (1200, 800)
    assert photos.urls(None, None, None) is None
    monkeypatch.delenv("ZIF_MEDIA_URL")
    assert photos.urls(key, 1200, 800) is None


# ZIF-133 F14. fence. Kills: a media.delete job kind that is not registered (the job would sit in
# the queue unclaimed, since CLAIM only returns the worker's kinds).
def test_media_delete_is_a_registered_job_kind() -> None:
    kind = worker.KINDS["media.delete"]
    assert (kind.handler, kind.timeout, kind.grace) == (
        photos.media_delete,
        30,
        timedelta(days=7),
    )


# ZIF-133 F10. fence. Kills: a progressive JPEG held to the baseline cap (it has no cheap draft, so
# 20 MP decodes whole and needs ~80 MB: two reviewers saw the 256Mi pod OOM on it).
def test_progressive_jpeg_over_the_common_cap_refused_before_decode(
    s3: S3Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = uuid.uuid4()
    upload_id = upload(s3, t, image("JPEG", (5000, 4000), progressive=True))
    no_load(monkeypatch)
    assert refusal(t, upload_id) == (422, "photo_unreadable")


# ZIF-133 F10. fence. Kills: a 16-bit grey PNG clipped to white instead of scaled to 8 bits.
def test_a_16_bit_grey_png_is_scaled_not_clipped(s3: S3Client) -> None:
    t = uuid.uuid4()
    buf = io.BytesIO()
    Image.new("I;16", (40, 40), 30000).save(buf, "PNG")
    v = photos.process(t, upload(s3, t, buf.getvalue()))
    grey = Image.open(io.BytesIO(v.detail)).convert("L").getpixel((20, 20))
    assert isinstance(grey, int) and abs(grey - 116) < 4


# ZIF-133 G1. guard: a corrupt embedded profile keeps the pixels instead of failing the photo.
def test_a_corrupt_icc_profile_keeps_the_pixels(s3: S3Client) -> None:
    t = uuid.uuid4()
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), (200, 100, 50)).save(buf, "PNG", icc_profile=b"not a profile" * 20)
    v = photos.process(t, upload(s3, t, buf.getvalue()))
    red, green, _ = Image.open(io.BytesIO(v.detail)).convert("RGB").getpixel((20, 20))  # type: ignore[misc]
    assert abs(red - 200) < 8 and abs(green - 100) < 8


MEASURE = """
import sys
from app import photos
data = open(sys.argv[1], "rb").read()
photos.variants(open(sys.argv[2], "rb").read())  # warm up: imports, codecs
if sys.platform == "darwin":
    import resource
    rss = lambda: resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    before = rss()
    photos.variants(data)
    print(rss() - before)
else:
    # ru_maxrss survives fork and exec, so a child of a big pytest worker starts at its peak.
    # Resetting the kernel's high-water mark is Linux's way to measure from here.
    status = lambda key: int(
        next(l for l in open("/proc/self/status") if l.startswith(key)).split()[1]
    ) * 1024
    try:
        open("/proc/self/clear_refs", "w").write("5")
    except OSError:
        print("unsupported")
        sys.exit(0)
    before = status("VmRSS:")
    photos.variants(data)
    print(status("VmHWM:") - before)
"""


# ZIF-133 G1. guard: peak memory of a decode, measured in a process of its own (the 256Mi pod OOMed
# on an 8 MP RGBA PNG and on a 100 MP JPEG before the pipeline shrank images early).
@pytest.mark.parametrize(
    ("name", "make"),
    [
        ("8 MP RGBA PNG", lambda: image_of("PNG", "RGBA", (3200, 2590))),
        ("100 MP baseline JPEG", lambda: image_of("JPEG", "RGB", (12500, 8000))),
    ],
)
def test_decoding_stays_inside_a_memory_budget(
    tmp_path: Path, name: str, make: Callable[[], bytes]
) -> None:
    (tmp_path / "big").write_bytes(make())
    (tmp_path / "small").write_bytes(image("PNG", (10, 10)))
    run = subprocess.run(
        [sys.executable, "-c", MEASURE, str(tmp_path / "big"), str(tmp_path / "small")],
        cwd=API_DIR,
        capture_output=True,
        text=True,
        env=os.environ,
    )
    assert run.returncode == 0, run.stderr
    if run.stdout.strip() == "unsupported":
        pytest.skip("this platform cannot reset a process's peak memory")
    peak = int(run.stdout) / 1e6
    print(f"{name}: peak above baseline {peak:.0f} MB")
    assert peak < 100


def image_of(fmt: str, mode: str, size: tuple[int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, (200, 100, 50, 255)[: len(mode)]).save(buf, fmt)
    return buf.getvalue()


# ZIF-133 F9. fence. Kills: a store failure answering 500, or a message quoting the URL or key.
def test_a_store_failure_is_a_503_and_logs_no_url(
    s3: S3Client, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    t = uuid.uuid4()
    upload_id = upload(s3, t, image("PNG", (10, 10)))
    real = storage.client

    def down(**kw: bool) -> S3Client:
        c = real(**kw)

        def fail(**_: object) -> None:
            raise EndpointConnectionError(endpoint_url=f"https://secret.example/{t}/{upload_id}")

        c.head_object = fail  # type: ignore[method-assign,assignment]
        c.put_object = fail  # type: ignore[method-assign,assignment]
        return c

    monkeypatch.setattr(storage, "client", down)
    with caplog.at_level(logging.WARNING):
        assert refusal(t, upload_id) == (503, "photos_unavailable")
        with pytest.raises(ApiError) as error:
            photos.finalize(t, uuid.uuid4(), upload_id)
        assert error.value.code == "photos_unavailable"
    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warned and all("secret.example" not in r.getMessage() + str(r.__dict__) for r in warned)


# ZIF-133 F9. fence. Kills: an upload that vanishes between the HEAD and the GET answering 500.
def test_an_upload_gone_after_the_head_is_missing(
    s3: S3Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = uuid.uuid4()
    monkeypatch.setattr(storage, "size", lambda key: 10)
    assert refusal(t, uuid.uuid4()) == (409, "photo_missing")


# ZIF-133 G1. guard: each client has a session of its own (the default one is not thread-safe).
def test_clients_do_not_use_the_default_session(
    s3: S3Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(boto3, "DEFAULT_SESSION", None)
    storage.client()
    assert boto3.DEFAULT_SESSION is None


# ZIF-133 F14. fence. Kills: a delete_objects answer carrying per-object Errors taken as success.
def test_media_delete_fails_when_the_store_reports_errors(
    s3: S3Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    s3.put_object(Bucket="ziftbook-media", Key="p/x", Body=b"x")
    c = storage.client()
    monkeypatch.setattr(c, "delete_objects", lambda **_: {"Errors": [{"Key": "p/x"}]})
    monkeypatch.setattr(storage, "client", lambda **_: c)
    with pytest.raises(RuntimeError):
        storage.delete_prefix("p/")


def jpeg_header(width: int, height: int, components: int, scan: int, sof: int = 0xC0) -> bytes:
    """SOI, an APP1 whose payload imitates a scan start, a frame header (SOF0 unless sof says
    otherwise) and the first SOS: no image data."""
    app1 = b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00"
    frame = (
        struct.pack(">BHHB", 8, height, width, components) + b"\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    )
    sos = bytes([scan]) + b"\x01\x00" * scan + b"\x00\x3f\x00"
    return (
        b"\xff\xd8"
        + b"\xff\xe1"
        + struct.pack(">H", len(app1) + 2)
        + app1
        + bytes([0xFF, sof])
        + struct.pack(">H", len(frame) + 2)
        + frame
        + b"\xff\xda"
        + struct.pack(">H", len(sos) + 2)
        + sos
    )


# ZIF-133 F10. fence. Kills: a multi-scan JPEG treated as cheap to decode (libjpeg buffers every
# coefficient of the image: +588 MB for 100 MP), or the walker reading an APP1's bytes as markers.
@pytest.mark.parametrize(
    ("data", "multi"),
    [
        (jpeg_header(10, 10, 3, 3), False),
        (jpeg_header(10, 10, 3, 1), True),
        (jpeg_header(10, 10, 3, 3)[:30], True),  # cut inside the frame header
        (b"\xff\xd8", True),
        (b"\xff\xd8\x00", True),
        (image("JPEG", (40, 40)), False),
        (image("JPEG", (40, 40), progressive=True), False),  # one SOF2, flagged by Pillow itself
    ],
    ids=["single", "multi", "truncated", "bare-soi", "no-marker", "pillow", "progressive"],
)
def test_multi_scan_detection(data: bytes, multi: bool) -> None:
    assert photos.multi_scan(data) is multi


def test_a_multi_scan_jpeg_over_the_common_cap_is_refused_before_decode(
    s3: S3Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = uuid.uuid4()
    upload_id = upload(s3, t, jpeg_header(3000, 2800, 3, 1) + b"\0" * 100)
    no_load(monkeypatch)
    assert refusal(t, upload_id) == (422, "photo_unreadable")


# ZIF-133 F10. fence. Kills: WebP held to the common cap (libwebp keeps ~4 full-size buffers: an
# 8.3 MP lossy RGBA WebP cost +176 MB).
def test_webp_has_its_own_cap(s3: S3Client, monkeypatch: pytest.MonkeyPatch) -> None:
    t = uuid.uuid4()
    ok = upload(s3, t, image("WEBP", (2000, 1950)))
    over = upload(s3, t, image("WEBP", (2500, 2000)))
    assert photos.process(t, ok).width == 1200
    no_load(monkeypatch)
    assert refusal(t, over) == (422, "photo_unreadable")


def test_pillow_carries_the_decoder_fixes() -> None:
    # ZB-33: 12.3.0 has a reachable JPEG heap overflow (CVE-2026-112403); 13.0.0 fixes it.
    major = int(PIL.__version__.split(".")[0])
    assert major >= 13


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    """One PNG chunk: length, type, payload and the CRC32 of type and payload."""
    crc = zlib.crc32(kind + payload)
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def png_with_chunk(kind: bytes, payload: bytes, size: tuple[int, int] = (4, 4)) -> bytes:
    """A PNG with one chunk inserted after IHDR (the signature and IHDR are 33 bytes)."""
    png = image_of("PNG", "RGB", size)
    return png[:33] + png_chunk(kind, payload) + png[33:]


# ZB-33 fence. Kills: a lossless or arithmetic-coded lossless JPEG handed to the decoder (libjpeg
# decodes it at full width whatever draft() asked for).
@pytest.mark.parametrize("sof", [0xC3, 0xC7, 0xCB, 0xCF])
def test_a_lossless_or_arithmetic_jpeg_is_unsafe(sof: int) -> None:
    assert photos.unsafe_container(jpeg_header(9600, 16, 1, 1, sof=sof))


def test_a_baseline_jpeg_is_safe() -> None:
    assert not photos.unsafe_container(image_of("JPEG", "RGB", (40, 30)))


# ZB-33 fence. Kills: an animated PNG reaching Image.open, which allocates its whole canvas.
def test_an_animated_png_is_unsafe() -> None:
    # An acTL chunk before IDAT marks an APNG (CVE-2026-112399 allocates on open).
    assert photos.unsafe_container(png_with_chunk(b"acTL", b"\x00\x00\x00\x02\x00\x00\x00\x00"))


# ZB-33 fence. Kills: a large eXIf chunk reaching exif_transpose (its cost grows with the chunk).
def test_a_png_with_a_big_exif_chunk_is_unsafe() -> None:
    assert photos.unsafe_container(png_with_chunk(b"eXIf", b"Exif\x00\x00" * 11_000))


def test_a_png_with_a_small_exif_chunk_is_safe() -> None:
    assert not photos.unsafe_container(png_with_chunk(b"eXIf", b"Exif\x00\x00II*\x00"))


# ZB-33 fence. Kills: the chunk walk stopping at IDAT. Pillow reads an eXIf chunk that follows the
# image data when it loads, and exif_transpose parses it like one before IDAT.
def test_a_big_exif_chunk_after_the_image_data_is_unsafe() -> None:
    png = image_of("PNG", "RGB", (4, 4))  # its last 12 bytes are IEND
    late = png[:-12] + png_chunk(b"eXIf", b"Exif\x00\x00" * 11_000) + png[-12:]
    assert photos.unsafe_container(late)


# ZB-33 fence. Kills: a refused container still opened (the check must come first in variants).
def test_variants_refuses_an_unsafe_container_before_opening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def never(*args: object, **kw: object) -> Image.Image:
        raise AssertionError("opened")

    monkeypatch.setattr(Image, "open", never)
    with pytest.raises(ApiError) as refused:
        photos.variants(jpeg_header(9600, 16, 1, 1, sof=0xC3))
    assert (refused.value.status_code, refused.value.code) == (422, "photo_unreadable")


LOSSLESS = jpeg_header(10, 10, 1, 1, sof=0xC3)
BASELINE = jpeg_header(10, 10, 3, 3)
PNG_IHDR = image_of("PNG", "RGB", (4, 4))[:33]


# ZB-33 guard: the walk runs on untrusted bytes, so a malformed structure is an answer, never an
# exception or an endless loop. A JPEG walk that cannot reach the first scan counts as unsafe
# (decoders skip bytes between markers that the walk stops at), as does FF00 before it (decoders
# skip it; the walk would read a length). A PNG is judged on the chunks before IEND.
@pytest.mark.parametrize(
    ("data", "unsafe"),
    [
        (b"", False),
        (b"hello, not an image", False),
        (image_of("WEBP", "RGB", (4, 4)), False),
        (b"\xff\xd8", True),
        (b"\xff\xd8\x00", True),
        (b"\xff\xd8\xff", True),
        (b"\xff\xd8\xff\xd9", True),
        (b"\xff\xd8\xff\xe1\xff\xff" + b"\0" * 10, True),
        (b"\xff\xd8\xff\xe1\x00\x00" + BASELINE[2:], True),
        (LOSSLESS[:20], True),
        (b"\xff\xd8\xff\xff\xff" + BASELINE[2:], False),
        (b"\xff\xd8\xff\xff\xff" + LOSSLESS[2:], True),
        (b"\xff\xd8\xff\x01\xff\xd0\xff\xd8" + BASELINE[2:], False),
        (b"\xff\xd8\xff\x01\xff\xd0\xff\xd8" + LOSSLESS[2:], True),
        (LOSSLESS[:16] + b"\x00" + LOSSLESS[16:], True),
        (b"\xff\xd8\xff\x00\x00\x02" + BASELINE[2:], True),
        (b"\x89PNG\r\n\x1a\n", False),
        (PNG_IHDR[:12], False),
        (PNG_IHDR + struct.pack(">I", 0x7FFFFFFF) + b"tEXt" + b"\0" * 20, False),
        (PNG_IHDR + struct.pack(">I", 0xFFFFFFFF) + b"eXIf", True),
        (PNG_IHDR + png_chunk(b"tEXt", b"") * 1000 + png_chunk(b"IEND", b""), False),
        (PNG_IHDR + png_chunk(b"tEXt", b"") * 1000 + png_chunk(b"acTL", b""), True),
        (png_with_chunk(b"eXIf", b"\0" * 65_536), False),
        (png_with_chunk(b"eXIf", b"\0" * 65_537), True),
        (image_of("PNG", "RGB", (4, 4)) + png_chunk(b"eXIf", b"\0" * 65_537), False),
    ],
    ids=[
        "empty",
        "junk",
        "webp",
        "jpeg-bare-soi",
        "jpeg-no-marker",
        "jpeg-cut-in-marker",
        "jpeg-eoi-before-scan",
        "jpeg-length-past-end",
        "jpeg-zero-length-segment",
        "jpeg-cut-in-frame",
        "jpeg-fill-bytes-baseline",
        "jpeg-fill-bytes-lossless",
        "jpeg-no-length-markers-baseline",
        "jpeg-no-length-markers-lossless",
        "jpeg-junk-before-frame",
        "jpeg-ff00-before-scan",
        "png-signature-only",
        "png-cut-in-chunk-header",
        "png-chunk-longer-than-data",
        "png-huge-exif-length",
        "png-empty-chunks",
        "png-actl-after-empty-chunks",
        "png-exif-at-64k",
        "png-exif-over-64k",
        "png-exif-after-iend",
    ],
)
def test_unsafe_container_answers_malformed_input(data: bytes, unsafe: bool) -> None:
    assert photos.unsafe_container(data) is unsafe


def exif_of(orientation: int, pad: int = 0) -> bytes:
    """EXIF (with its Exif\\0\\0 header) holding an orientation, padded after its IFD."""
    exif = Image.Exif()
    exif[0x0112] = orientation
    return exif.tobytes() + b"\0" * pad


def raw_profile(exif: bytes) -> bytes:
    """EXIF as PNG text "Raw profile type exif" holds it: hex lines after a name and a length."""
    hexed = exif.hex()
    lines = [hexed[i : i + 72] for i in range(0, len(hexed), 72)]
    return f"\nexif\n{len(exif):8d}\n".encode() + "\n".join(lines).encode() + b"\n"


def exif_carriers(exif: bytes) -> list[bytes]:
    """A 40x20 photo carrying exif in each place, other than a PNG eXIf chunk or a JPEG, that
    Pillow reads it from."""
    late = image_of("PNG", "RGB", (40, 20))  # its last 12 bytes are IEND
    return [
        png_with_chunk(b"tEXt", b"exif\0" + exif, (40, 20)),
        late[:-12] + png_chunk(b"tEXt", b"exif\0" + exif) + late[-12:],
        png_with_chunk(
            b"zTXt", b"Raw profile type exif\0\0" + zlib.compress(raw_profile(exif)), (40, 20)
        ),
        png_with_chunk(b"iTXt", b"Raw profile type exif\0\0\0\0\0" + raw_profile(exif), (40, 20)),
        image("WEBP", (40, 20), exif=exif),
    ]


CARRIERS = [
    "png-text",
    "png-text-after-idat",
    "png-ztxt-raw-profile",
    "png-itxt-raw-profile",
    "webp",
]


# ZB-33 fence. Kills: EXIF over the cap reaching exif_transpose from PNG text, a WebP EXIF chunk,
# or a chunk after the image data (it reaches im.info only on load).
@pytest.mark.parametrize("data", exif_carriers(exif_of(1, pad=70_000)), ids=CARRIERS)
def test_exif_over_the_cap_is_refused_before_it_is_parsed(
    data: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    def never(*args: object, **kw: object) -> None:
        raise AssertionError("parsed")

    monkeypatch.setattr(ImageOps, "exif_transpose", never)
    with pytest.raises(ApiError) as refused:
        photos.variants(data)
    assert (refused.value.status_code, refused.value.code) == (422, "photo_unreadable")


# ZB-33 guard: EXIF under the cap still turns the photo, wherever Pillow read it from.
@pytest.mark.parametrize("data", exif_carriers(exif_of(6)), ids=CARRIERS)
def test_small_exif_still_turns_the_photo(data: bytes) -> None:
    v = photos.variants(data)
    assert (v.width, v.height) == (20, 40)


def jpeg_with_exif(*sizes: int) -> bytes:
    """A JPEG with one APP1 EXIF segment per size (its payload, Exif header included)."""
    jpeg = image_of("JPEG", "RGB", (40, 30))
    app1 = b"".join(
        b"\xff\xe1" + struct.pack(">H", n + 2) + b"Exif\0\0" + b"\0" * (n - 6) for n in sizes
    )
    return jpeg[:2] + app1 + jpeg[2:]


# ZB-33 fence. Kills: a JPEG's EXIF left uncapped. Pillow joins its APP1 EXIF segments and parses
# them inside Image.open (for the dpi), so the cap must come before it.
def test_a_jpeg_whose_exif_segments_add_up_over_the_cap_is_unsafe() -> None:
    assert not photos.unsafe_container(jpeg_with_exif(65_533))  # the most one segment holds
    assert photos.unsafe_container(jpeg_with_exif(40_000, 40_000))


def malformed_exif(tag: int, value: object, typ: int, count: int | None = None) -> bytes:
    """EXIF holding orientation 6 and one more tag, with that tag's stored type (and count)
    rewritten. Image.Exif writes big-endian entries of 12 bytes from byte 16."""
    exif = Image.Exif()
    exif[0x0112] = 6
    exif[tag] = value
    data = bytearray(exif.tobytes())
    at = next(i for i in range(16, len(data), 12) if data[i : i + 2] == tag.to_bytes(2))
    data[at + 2 : at + 4] = typ.to_bytes(2)
    if count is not None:
        data[at + 4 : at + 8] = count.to_bytes(4)
    return bytes(data)


# ZB-33 fence. Kills: EXIF that Pillow cannot read or re-encode answering 500. exif_transpose
# rewrites the EXIF after turning the photo, and Pillow 13 raises on a value that does not fit its
# tag (struct.error, AttributeError, TypeError), on a header that is no TIFF (SyntaxError) and on
# a raw profile that is no hex (ValueError).
@pytest.mark.parametrize(
    "data",
    [
        png_with_chunk(b"tEXt", b"exif\0" + malformed_exif(0x0128, 2, typ=2), (40, 20)),
        png_with_chunk(
            b"tEXt", b"exif\0" + malformed_exif(0x010F, "Camera", typ=5, count=1), (40, 20)
        ),
        png_with_chunk(b"tEXt", b"exif\0" + malformed_exif(0x011A, 72.0, typ=2), (40, 20)),
        png_with_chunk(b"tEXt", b"exif\0Exif\0\0" + b"\0" * 16, (40, 20)),
        png_with_chunk(b"tEXt", b"Raw profile type exif\0\nexif\n       4\nzzzzzzzz\n", (40, 20)),
    ],
    ids=[
        "short-stored-as-text",
        "text-stored-as-a-rational",
        "rational-stored-as-text",
        "not-tiff",
        "raw-profile-not-hex",
    ],
)
def test_exif_pillow_cannot_read_is_unreadable(data: bytes) -> None:
    with pytest.raises(ApiError) as refused:
        photos.variants(data)
    assert (refused.value.status_code, refused.value.code) == (422, "photo_unreadable")
