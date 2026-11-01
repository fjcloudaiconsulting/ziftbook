"""Service photos (ZIF-133): the browser PUTs the original to the uploads bucket on a presigned URL,
the API checks it (type from the bytes, size, pixels), re-encodes two WebP variants without
metadata and links them to a service when the owner saves it (services.py).

The decode never runs in this process (ZIF-230): each one is a child process of its own
(photo_child.py, photo_decode.py) under resource limits, so a decoder fault or a bomb ends the
child, not the API. This module never imports Pillow, and trusts nothing the child writes."""

import logging
import os
import re
import resource
import selectors
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import IO, Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from opentelemetry.trace import Span, SpanKind
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from sqlalchemy import text

from app import auth, limits, photo_child, storage, tracing
from app.auth import CurrentOwner, SignedIn
from app.config import StorageSettings
from app.db import tenant_context
from app.errors import ApiError, Error
from app.jobs import Job

MAX_BYTES = photo_child.MAX_BYTES
TYPES = ("image/jpeg", "image/png", "image/webp")
# One decode at a time (held from the read of the upload): one child per API process.
_LOCK = threading.Lock()
LOCK_WAIT = 15  # seconds, then 503 busy
CHILD = Path(photo_child.__file__)
# Wall clock for a child, from its start to its exit. Twice its CPU limit: a costly photo meets
# RLIMIT_CPU first, so this only ends a child that was stalled or starved of CPU.
DECODE_TIMEOUT = 10.0
REAP_GRACE = 5.0  # seconds a killed child gets to be reaped before it is left for later
# Past the deadline, a child that had this much CPU was the photo's fault (422); less, the node's
# (503 busy). Half the soft CPU limit, more than twice a legitimate decode.
STARVED_CPU_MS = 2500
# The record's cap (header and both variants). The largest legitimate output measured is 2.6 MB
# (an RGBA noise photo).
OUTPUT_CAP = 4 * 1024 * 1024
HEADER_CAP = 1024
MAX_PIXELS = 100_000_000  # photo_decode.JPEG_CAP: the largest photo it accepts
# A refusal from a child this close to its address-space limit is counted as memory: libwebp and
# Pillow report a failed allocation as an ordinary encoding error.
MEMORY_NEAR = 0.9
CHUNK = 64 * 1024
# The pod's memory has to hold the API and a child at its ceiling: where the container's cgroup
# has memory.oom.group (the kubelet sets it on cgroup v2), a child OOM restarts the whole pod,
# whatever its oom_score_adj. API_ALLOWANCE is the API's own share (measured 142 MiB after startup
# on arm64; the pod manifests note 152 MB). See budget() and _headroom().
API_ALLOWANCE = 160 * 1024 * 1024
BUDGET_MARGIN = 16 * 1024 * 1024
CGROUP = Path("/sys/fs/cgroup")
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
            # The HEAD answers early; the read after it is capped on its own (ZIF-230).
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
                with tempfile.TemporaryFile() as upload:
                    try:
                        got = storage.read_into(key, upload, MAX_BYTES)
                    except storage.TooLarge:
                        raise ApiError(422, "photo_too_large") from None
                    if got is None:
                        raise ApiError(409, "photo_missing")
                    upload.seek(0)
                    found = variants(upload, upload_id, span)
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
    logger.info("photo refused", extra={"code": refused.code, **getattr(refused, "fields", {})})
    raise refused


class Refusal(ApiError):
    """A refusal that carries what the decode said about it, for the `photo refused` line."""

    def __init__(self, status_code: int, code: str, fields: dict[str, Any]) -> None:
        super().__init__(status_code, code)
        self.fields = fields


@dataclass(frozen=True)
class Run:
    """One child, ended and reaped (or given up on)."""

    returncode: int | None  # None: never reaped
    output: bytes  # all of stdout, the ready marker included, at most OUTPUT_CAP + 1 + CHUNK
    killed: Literal["timeout", "output"] | None  # why this process ended it, if it did
    cpu_ms: int  # the child's own CPU time, from its rusage (0 if never reaped)


# Children killed but not yet reaped; each later run tries again (photo decoder not reaped).
_UNREAPED: list[subprocess.Popen[bytes]] = []


def _cgroup_int(name: str) -> int | None:
    """An integer from this container's cgroup v2 file; None if it is absent, "max" or unreadable
    (compose without a limit, cgroup v1, macOS)."""
    try:
        value = (CGROUP / name).read_text().strip()
    except OSError:
        return None
    return int(value) if value.isdigit() else None


def budget() -> Literal["ok", "tight", "unlimited"]:
    """At startup (main.py): whether the container's memory limit holds the API, a child at its
    ceiling and its output. Too small is logged as an ERROR, never a refusal to start; _headroom
    then turns each decode it cannot hold into a 503."""
    memory_max = _cgroup_int("memory.max")
    if memory_max is None:
        return "unlimited"
    required = API_ALLOWANCE + photo_child.MEMORY + OUTPUT_CAP + BUDGET_MARGIN
    if memory_max >= required:
        return "ok"
    oom_group = _cgroup_int("memory.oom.group")
    logger.error(
        "photo decoder memory budget too small",
        extra={"memory_max": memory_max, "required": required, "oom_group": oom_group},
    )
    return "tight"


def _rss() -> int:
    """This process's resident memory in bytes (0 where there is no /proc). statm stays readable
    when the process is not dumpable."""
    try:
        with open("/proc/self/statm") as statm:
            return int(statm.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except OSError:
        return 0


def _headroom() -> dict[str, int] | None:
    """None when a child at its ceiling fits in the container's memory beside this process now;
    otherwise the numbers, for the log."""
    memory_max = _cgroup_int("memory.max")
    if memory_max is None:
        return None
    rss = _rss()
    if memory_max - rss >= photo_child.MEMORY + OUTPUT_CAP + BUDGET_MARGIN:
        return None
    return {"memory_max": memory_max, "rss": rss}


def _spawn(upload: IO[bytes]) -> subprocess.Popen[bytes]:
    """The child: no environment (the API's secrets stay here), no descriptor but the upload, a
    pipe and /dev/null, -I so no PYTHON* variable, user site or working directory reaches it, -B so
    it writes no bytecode. No preexec_fn: nothing runs between fork and exec in a threaded API."""
    return subprocess.Popen(
        [sys.executable, "-I", "-B", str(CHILD)],
        stdin=upload,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={},
        cwd="/",
        close_fds=True,
    )


def _reap(proc: subprocess.Popen[bytes], deadline: float) -> resource.struct_rusage | None:
    """Wait for the child until deadline (wait4, for its own rusage); None if it has not exited."""
    while True:
        pid, status, used = os.wait4(proc.pid, os.WNOHANG)
        if pid:
            proc.returncode = os.waitstatus_to_exitcode(status)
            return used
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.005)


def _collect(
    proc: subprocess.Popen[bytes], deadline: float
) -> tuple[bytes, Literal["timeout", "output"] | None]:
    """stdout until EOF, the deadline ("timeout") or the cap ("output"): never more than
    OUTPUT_CAP + 1 + CHUNK bytes in memory. The child never reads from this process (its stdin
    is a file), so reading one pipe cannot deadlock."""
    assert proc.stdout is not None
    fd, out = proc.stdout.fileno(), bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return bytes(out), "timeout"
            if not selector.select(left):
                continue
            chunk = os.read(fd, CHUNK)
            if not chunk:
                return bytes(out), None
            out += chunk
            if len(out) > len(photo_child.READY) + OUTPUT_CAP:
                return bytes(out), "output"


def _run(upload: IO[bytes]) -> Run:
    """Start a child on the upload and see it end: one deadline covers its output and its exit,
    and a child still running past it (or past the output cap) is killed and reaped."""
    for left in list(_UNREAPED):
        if _reap(left, time.monotonic()) is not None:
            _UNREAPED.remove(left)
    deadline = time.monotonic() + DECODE_TIMEOUT
    proc = _spawn(upload)
    output, used = b"", None
    killed: Literal["timeout", "output"] | None = None
    try:
        output, killed = _collect(proc, deadline)
        if killed is None:
            used = _reap(proc, deadline)
            if used is None:  # stdout closed, still running
                killed = "timeout"
    finally:
        if proc.returncode is None:
            proc.kill()
            used = _reap(proc, time.monotonic() + REAP_GRACE)
            if used is None:
                logger.error("photo decoder not reaped")
                _UNREAPED.append(proc)
        assert proc.stdout is not None
        proc.stdout.close()
    cpu_ms = round((used.ru_utime + used.ru_stime) * 1000) if used else 0
    return Run(proc.returncode, output, killed, cpu_ms)


FORMATS = Literal["JPEG", "PNG", "WEBP"]


class Decoded(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    kind: Literal["ok"]
    format: FORMATS
    pixels: Annotated[int, Field(ge=1, le=MAX_PIXELS)]
    width: Annotated[int, Field(ge=1, le=1200)]
    height: Annotated[int, Field(ge=1, le=1200)]
    thumb: Annotated[int, Field(ge=1)]
    detail: Annotated[int, Field(ge=1)]
    peak_kb: Annotated[int, Field(ge=0)]
    cpu_ms: Annotated[int, Field(ge=0)]


class Refused(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    kind: Literal["refused"]
    code: Literal["photo_type", "photo_unreadable"]
    # A class or check name: it goes into a log line as it is.
    error: Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")]
    format: FORMATS | None
    pixels: Annotated[int, Field(ge=1, le=2**62)] | None
    peak_kb: Annotated[int, Field(ge=0)]
    cpu_ms: Annotated[int, Field(ge=0)]


_RECORD: TypeAdapter[Decoded | Refused] = TypeAdapter(
    Annotated[Decoded | Refused, Field(discriminator="kind")]
)
# A still WebP as Pillow writes one with no metadata: VP8X only to say there is alpha.
WEBP_LAYOUTS = {
    (b"VP8 ",),
    (b"VP8L",),
    (b"VP8X", b"VP8 "),
    (b"VP8X", b"ALPH", b"VP8 "),
    (b"VP8X", b"VP8L"),
}
VP8X_ALPHA = 0x10


def webp_size(blob: bytes) -> tuple[int, int] | None:
    """The size a still WebP with no metadata declares, read from its structure and never decoded;
    None for anything else: a RIFF size that is not the blob's, a chunk outside WEBP_LAYOUTS (so
    no EXIF, XMP, ICCP or animation), a VP8X flag other than alpha, or a canvas that is not the
    frame's size. The variants are served publicly: the child is not trusted to have made them."""
    if len(blob) < 20 or blob[:4] != b"RIFF" or blob[8:12] != b"WEBP":
        return None
    if int.from_bytes(blob[4:8], "little") != len(blob) - 8:
        return None
    chunks, i = [], 12
    while i < len(blob):
        n = int.from_bytes(blob[i + 4 : i + 8], "little")
        if i + 8 + n > len(blob):
            return None
        chunks.append((blob[i : i + 4], blob[i + 8 : i + 8 + n]))
        i += 8 + n + (n & 1)
    if i != len(blob) or tuple(kind for kind, _ in chunks) not in WEBP_LAYOUTS:
        return None
    canvas = frame = None
    for kind, payload in chunks:
        if kind == b"VP8X":
            if len(payload) != 10 or payload[0] & ~VP8X_ALPHA:
                return None
            canvas = (
                int.from_bytes(payload[4:7], "little") + 1,
                int.from_bytes(payload[7:10], "little") + 1,
            )
        elif kind == b"VP8 ":
            if len(payload) < 10 or payload[3:6] != b"\x9d\x01\x2a":
                return None
            frame = (
                int.from_bytes(payload[6:8], "little") & 0x3FFF,
                int.from_bytes(payload[8:10], "little") & 0x3FFF,
            )
        elif kind == b"VP8L":
            if len(payload) < 5 or payload[0] != 0x2F:
                return None
            bits = int.from_bytes(payload[1:5], "little")
            frame = ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    if frame is None or (canvas is not None and canvas != frame):
        return None
    return frame


def parse(body: bytes) -> tuple[Decoded | Refused, bytes, bytes] | None:
    """The child's record (after the ready marker): None unless every part checks out."""
    n = int.from_bytes(body[:4])
    if len(body) < 4 or n > HEADER_CAP or len(body) < 4 + n:
        return None
    try:
        head = _RECORD.validate_json(body[4 : 4 + n])
    except ValidationError:
        return None
    blobs = body[4 + n :]
    if isinstance(head, Refused):
        return (head, b"", b"") if not blobs else None
    if len(blobs) != head.thumb + head.detail:
        return None
    thumb, detail = blobs[: head.thumb], blobs[head.thumb :]
    if webp_size(thumb) != (320, 320) or webp_size(detail) != (head.width, head.height):
        return None
    return head, thumb, detail


# Exit codes a child gives before its ready marker when it could not start (2 is the
# interpreter's own: no such script). After the marker, a code is the decode's fault.
START_FAILURES = {2: "import", photo_child.EXIT_LIMITS: "limits", photo_child.EXIT_IMPORT: "import"}


def _judge(run: Run) -> tuple[str, Decoded | Refused | None, bytes, bytes]:
    """The outcome of a run (ok, refused, or why it failed) and the record when there is one."""
    ready = run.output[: len(photo_child.READY)] == photo_child.READY
    if not ready and run.killed is None and run.returncode in START_FAILURES:
        return START_FAILURES[run.returncode], None, b"", b""
    if run.killed == "output":
        return "output", None, b"", b""
    if run.killed == "timeout":
        return ("timeout" if run.cpu_ms >= STARVED_CPU_MS else "starved"), None, b"", b""
    code = run.returncode
    if code == 0 and ready:
        parsed = parse(run.output[len(photo_child.READY) :])
        if parsed is None:
            return "protocol", None, b"", b""
        head, thumb, detail = parsed
        if isinstance(head, Refused) and head.peak_kb * 1024 >= MEMORY_NEAR * photo_child.MEMORY:
            return "memory", head, b"", b""
        return ("ok" if isinstance(head, Decoded) else "refused"), head, thumb, detail
    if code == 0:
        return "protocol", None, b"", b""
    if code == -signal.SIGXCPU:
        return "cpu", None, b"", b""
    if code == -signal.SIGKILL:  # not this process's: the CPU hard limit, or the OOM killer
        return ("cpu" if run.cpu_ms >= photo_child.CPU[1] * 1000 - 100 else "oom"), None, b"", b""
    if code is not None and code < 0:
        return "signal", None, b"", b""
    if code == photo_child.EXIT_MEMORY:
        return "memory", None, b"", b""
    return "exit", None, b"", b""


def variants(upload: IO[bytes], upload_id: UUID, span: Span) -> Variants:
    """Decode an upload in a child process (_run), with the lock held (process). The answers: the
    variants, the child's own refusal (422), 422 photo_unreadable when the child failed (killed by a
    limit, crashed, wrote too much or nothing usable), 503 busy when it could not start or was
    starved of CPU (the node's fault, not the photo's). Logs ids and closed sets only."""
    short = _headroom()
    if short is not None:  # a child at its ceiling could OOM the whole pod
        fields: dict[str, Any] = {"decoder": "headroom", "upload_id": str(upload_id), **short}
        span.set_attribute("decoder", "headroom")
        logger.error("photo decoder could not start", extra=fields)
        raise Refusal(503, "busy", fields)
    try:
        run = _run(upload)
    except OSError:  # no process: out of memory or pids, or no interpreter
        run = None
    outcome, head, thumb, detail = ("spawn", None, b"", b"") if run is None else _judge(run)
    span.set_attribute("decoder", outcome)
    if head is not None:
        span.set_attributes({"format": str(head.format), "pixels": head.pixels or 0})
    fields = {"decoder": outcome, "upload_id": str(upload_id)}
    if run is not None:
        fields["cpu_ms"] = run.cpu_ms
        if run.returncode is not None and run.returncode < 0:
            fields["signal"] = -run.returncode
        elif run.returncode is not None:
            fields["exit_code"] = run.returncode
    if head is not None:
        fields["peak_kb"] = head.peak_kb
    if isinstance(head, Decoded) and outcome == "ok":
        return Variants(thumb, detail, head.width, head.height)
    if isinstance(head, Refused) and outcome == "refused":
        raise Refusal(422, head.code, {**fields, "error": head.error})
    if outcome in ("spawn", "limits", "import"):
        logger.error("photo decoder could not start", extra=fields)
        raise Refusal(503, "busy", fields)
    logger.warning("photo decoder failed", extra=fields)
    if outcome == "starved":
        raise Refusal(503, "busy", fields)
    raise Refusal(422, "photo_unreadable", fields)


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
