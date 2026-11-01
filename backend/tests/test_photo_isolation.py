"""ZIF-230: the photo decode runs in a child process under resource limits, and the API trusts
nothing it writes. tests/photo_child_probe.py stands in for the child where a test needs it to
misbehave. None of these tests may skip on Linux unless the test runs as root (conftest.py).
They need no database, so the image's photo-check stage runs them on each architecture."""

import ast
import io
import json
import logging
import os
import random
import resource
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from mypy_boto3_s3 import S3Client
from opentelemetry import trace
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from PIL import Image

from app import photo_child, photo_decode, photos
from app.errors import ApiError
from tests import photo_syscalls
from tests.conftest import API_DIR, closed, image, points, upload

PROBE = Path(__file__).with_name("photo_child_probe.py")
LINUX = sys.platform == "linux"
ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
only_linux = pytest.mark.skipif(not LINUX, reason="needs Linux: /proc, RLIMIT_AS or seccomp")


def decode(data: bytes, child: Path = PROBE) -> photos.Variants:
    """photos.variants on data, run by child (the probe unless said otherwise)."""
    previous = photos.CHILD
    photos.CHILD = child
    try:
        with tempfile.TemporaryFile() as upload_file:
            upload_file.write(data)
            upload_file.seek(0)
            return photos.variants(upload_file, uuid.uuid4(), trace.INVALID_SPAN)
    finally:
        photos.CHILD = previous


def refused(data: bytes, child: Path = PROBE) -> tuple[int, str]:
    with pytest.raises(ApiError) as error:
        decode(data, child)
    return error.value.status_code, error.value.code


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch) -> list[photos.Run]:
    """Every child run in this test, once it has ended."""
    seen: list[photos.Run] = []
    real = photos._run

    def spy(upload_file: Any) -> photos.Run:
        run = real(upload_file)
        seen.append(run)
        return run

    monkeypatch.setattr(photos, "_run", spy)
    return seen


@pytest.fixture
def failed(caplog: pytest.LogCaptureFixture) -> Iterator[Callable[[], list[dict[str, Any]]]]:
    """The fields of each `photo decoder failed` / `could not start` line logged so far."""
    with caplog.at_level(logging.INFO, logger="app.photos"):
        yield lambda: [
            {"msg": r.getMessage(), **r.__dict__}
            for r in caplog.records
            if r.getMessage() in ("photo decoder failed", "photo decoder could not start")
        ]


PNG = image("PNG", (20, 10))


def probe(mode: str, rest: bytes = b"") -> bytes:
    return mode.encode() + b"\n" + rest


# ZIF-230 fence. Kills: a decode in the API process (Pillow patched to fail here, the photo
# still decodes: in the child).
def test_the_api_never_decodes_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    def never(*args: object, **kw: object) -> None:
        raise AssertionError("decoded in the API process")

    monkeypatch.setattr(Image, "open", never)
    found = decode(PNG, photos.CHILD)
    assert (found.width, found.height) == (20, 10)


# ZIF-230 fence. Kills: Pillow back in the API or the worker process.
def test_the_api_does_not_import_pillow() -> None:
    script = "import sys, app.main, app.worker; print('PIL' in sys.modules)"
    run = subprocess.run(
        [sys.executable, "-c", script], cwd=API_DIR, capture_output=True, text=True, check=True
    )
    assert run.stdout.strip() == "False"


def vm_rss() -> int:
    with open("/proc/self/status") as status:
        return next(int(line.split()[1]) for line in status if line.startswith("VmRSS:")) * 1024


# ZIF-230 fence. Kills: no address-space limit on the child, or a memory fault answered as anything
# but the undecodable photo's code. The 1 GiB lands in the child, never in this process. The API
# route version is in test_service_photos_api.py.
@only_linux
@pytest.mark.parametrize("mode", ["probe-memory", "probe-native-memory"])
def test_a_decode_over_the_memory_limit_kills_only_the_child(
    mode: str, runs: list[photos.Run], failed: Callable[[], list[dict[str, Any]]]
) -> None:
    before = vm_rss()
    assert refused(probe(mode)) == (422, "photo_unreadable")
    assert vm_rss() - before < 100 * 1024 * 1024
    assert [r.returncode for r in runs] == [photo_child.EXIT_MEMORY]
    assert [(f["decoder"], f["exit_code"]) for f in failed()] == [("memory", 5)]
    assert decode(PNG, photos.CHILD).width == 20  # the next photo decodes


# ZIF-230 fence. Kills: a refusal from a child at its memory limit logged as an ordinary refusal
# (libwebp and Pillow report a failed allocation as an encoding error).
@only_linux
def test_a_refusal_near_the_memory_limit_counts_as_memory(
    failed: Callable[[], list[dict[str, Any]]],
) -> None:
    assert refused(probe("probe-near-limit")) == (422, "photo_unreadable")
    [line] = failed()
    assert line["decoder"] == "memory"
    assert line["peak_kb"] * 1024 >= photos.MEMORY_NEAR * photo_child.MEMORY


# ZIF-230 fence. Kills: a native crash in the decoder taking the API down, or answering 500.
def test_a_decoder_crash_is_photo_unreadable(
    failed: Callable[[], list[dict[str, Any]]],
) -> None:
    assert refused(probe("probe-crash")) == (422, "photo_unreadable")
    [line] = failed()
    assert (line["decoder"], line["signal"]) in (("signal", signal.SIGSEGV), ("signal", 10))


# ZIF-230 fence. Kills: no CPU limit on the child (the probe lowers it to 1 s: the wall clock is
# not what ends it).
def test_a_decode_past_its_cpu_limit_is_killed(
    failed: Callable[[], list[dict[str, Any]]],
) -> None:
    started = time.monotonic()
    assert refused(probe("probe-cpu")) == (422, "photo_unreadable")
    assert time.monotonic() - started < photos.DECODE_TIMEOUT
    [line] = failed()
    assert (line["decoder"], line["signal"]) == ("cpu", signal.SIGXCPU)


# ZIF-230 fence (build review, correctness 2). Kills: an unsent SIGKILL classed wrong. A child past
# its soft CPU limit got SIGXCPU and ran on, so a SIGKILL then is the hard limit's (cpu); one with
# little CPU is the OOM killer's (oom). The line is the soft limit, not a hair under the hard one.
def test_an_unsent_sigkill_is_cpu_or_oom_by_the_childs_cpu() -> None:
    soft = photo_child.CPU[0] * 1000

    def outcome(code: int, cpu_ms: int) -> str:
        return photos._judge(photos.Run(code, photo_child.READY, None, cpu_ms))[0]

    assert outcome(-signal.SIGKILL, soft) == "cpu"
    assert outcome(-signal.SIGKILL, soft + 500) == "cpu"
    assert outcome(-signal.SIGKILL, soft - 1) == "oom"
    assert outcome(-signal.SIGKILL, 100) == "oom"
    assert outcome(-signal.SIGXCPU, 100) == "cpu"


@only_linux  # macOS sends no SIGKILL at the hard limit
def test_a_child_that_ignores_sigxcpu_is_killed_at_the_hard_limit(
    monkeypatch: pytest.MonkeyPatch, runs: list[photos.Run]
) -> None:
    monkeypatch.setattr(photo_child, "CPU", (1, 2))  # as the probe sets its own
    refused(probe("probe-ignore-xcpu"))
    [run] = runs
    assert (run.killed, run.returncode) == (None, -signal.SIGKILL)
    assert photos._judge(run)[0] == "cpu"


# ZIF-230 fence (ruling D2). Kills: an unbounded wait, a leaked child, or the wall clock answered
# without asking whose fault it was: a child that used its time is the photo's (422), one that
# got no CPU is the node's (503 busy). Closing stdout first does not escape the deadline.
@pytest.mark.parametrize(
    ("mode", "answer", "outcome"),
    [
        ("probe-sleep", (503, "busy"), "starved"),
        ("probe-close-then-sleep", (503, "busy"), "starved"),
        ("probe-spin", (422, "photo_unreadable"), "timeout"),
        ("probe-close-then-spin", (422, "photo_unreadable"), "timeout"),
    ],
)
def test_a_child_past_its_deadline_is_killed_and_reaped(
    mode: str,
    answer: tuple[int, str],
    outcome: str,
    monkeypatch: pytest.MonkeyPatch,
    runs: list[photos.Run],
    failed: Callable[[], list[dict[str, Any]]],
) -> None:
    monkeypatch.setattr(photos, "DECODE_TIMEOUT", 1.5)
    monkeypatch.setattr(photos, "STARVED_CPU_MS", 300)
    started = time.monotonic()
    assert refused(probe(mode)) == answer
    assert time.monotonic() - started < photos.DECODE_TIMEOUT + 2
    assert [(r.killed, r.returncode) for r in runs] == [("timeout", -signal.SIGKILL)]
    assert [f["decoder"] for f in failed()] == [outcome]


# ZIF-230 fence (build review ruling). Kills: no backstop for a child its parent no longer watches
# (the API died mid-decode under a dev reloader, where it is not PID 1): the child's own alarm ends
# it. The probe brings the alarm forward to 1 s; the parent's deadline is 10 s.
def test_the_child_ends_itself_at_its_alarm(runs: list[photos.Run]) -> None:
    started = time.monotonic()
    refused(probe("probe-alarm"))
    assert time.monotonic() - started < 5
    assert [(r.killed, r.returncode) for r in runs] == [(None, -signal.SIGALRM)]


# ZIF-230 fence. Kills: the child's output read without a cap into the API's memory.
def test_decoder_output_is_capped(
    runs: list[photos.Run], failed: Callable[[], list[dict[str, Any]]]
) -> None:
    assert refused(probe("probe-flood")) == (422, "photo_unreadable")
    [run] = runs
    assert (run.killed, run.returncode) == ("output", -signal.SIGKILL)
    assert len(run.output) <= len(photo_child.READY) + photos.OUTPUT_CAP + photos.CHUNK
    assert [f["decoder"] for f in failed()] == ["output"]


# ZIF-230 fence (build review, correctness 1). Kills: the kill reaping a child that exited after the
# parent last looked (Popen.kill polls first), which lost its outcome and rusage, answered 503
# "could not start" (a forgeable page) and leaked the pipe. The parent is slowed past the exit.
def test_a_child_that_exits_before_the_kill_keeps_its_outcome(
    monkeypatch: pytest.MonkeyPatch, runs: list[photos.Run]
) -> None:
    spawned: list[subprocess.Popen[bytes]] = []
    real_spawn, real_collect = photos._spawn, photos._collect

    def spawn(upload_file: Any) -> subprocess.Popen[bytes]:
        spawned.append(real_spawn(upload_file))
        return spawned[-1]

    def slow_collect(proc: Any, deadline: float) -> Any:
        found = real_collect(proc, deadline)
        time.sleep(0.2)  # the child exits meanwhile
        return found

    monkeypatch.setattr(photos, "_spawn", spawn)
    monkeypatch.setattr(photos, "_collect", slow_collect)
    assert refused(probe(f"probe-overflow-exit:{photos.OUTPUT_CAP}")) == (422, "photo_unreadable")
    [run] = runs
    assert (run.killed, run.returncode) == ("output", 0)
    assert run.cpu_ms > 0  # its own rusage, read when this module reaped it
    [proc] = spawned
    assert proc.stdout is not None and proc.stdout.closed


# ZIF-230 fence (build review, correctness 8). Kills: a killed child that does not exit within the
# grace dropped (a zombie for good) or holding up the request: it is logged, kept, its pipe
# closed, and the next decode reaps it.
def test_a_child_not_reaped_in_time_is_kept_and_reaped_later(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(photos, "DECODE_TIMEOUT", 0.5)
    spawned: list[subprocess.Popen[bytes]] = []
    real_spawn, real_reap = photos._spawn, photos._reap
    calls = 0

    def spawn(upload_file: Any) -> subprocess.Popen[bytes]:
        spawned.append(real_spawn(upload_file))
        return spawned[-1]

    def slow_reap(proc: Any, deadline: float) -> Any:
        nonlocal calls
        calls += 1
        return None if calls == 1 else real_reap(proc, deadline)

    monkeypatch.setattr(photos, "_spawn", spawn)
    monkeypatch.setattr(photos, "_reap", slow_reap)
    monkeypatch.setattr(photos, "_UNREAPED", [])
    with caplog.at_level(logging.ERROR, logger="app.photos"):
        refused(probe("probe-sleep"))
    assert "photo decoder not reaped" in [r.getMessage() for r in caplog.records]
    first = spawned[0]
    assert photos._UNREAPED == [first]
    assert first.stdout is not None and first.stdout.closed
    time.sleep(0.2)  # killed: it exits
    assert decode(PNG, photos.CHILD).width == 20
    assert photos._UNREAPED == [] and first.returncode == -signal.SIGKILL


# ZIF-230 guard: the pipe is closed on every path, a failing reap included.
def test_the_pipe_is_closed_when_the_reap_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    spawned: list[subprocess.Popen[bytes]] = []
    real_spawn = photos._spawn

    def spawn(upload_file: Any) -> subprocess.Popen[bytes]:
        spawned.append(real_spawn(upload_file))
        return spawned[-1]

    def broken(proc: Any, deadline: float) -> None:
        raise RuntimeError("reap failed")

    monkeypatch.setattr(photos, "_spawn", spawn)
    monkeypatch.setattr(photos, "_reap", broken)
    monkeypatch.setattr(photos, "DECODE_TIMEOUT", 0.5)
    with pytest.raises(RuntimeError):
        decode(probe("probe-sleep"))
    [proc] = spawned
    assert proc.stdout is not None and proc.stdout.closed
    proc.kill()
    proc.wait()


# ZIF-230 guard: a child something else reaped is no error, and keeps the outcome recorded for it.
def test_a_child_reaped_elsewhere_keeps_its_outcome() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(7)"])
    proc.wait()
    assert photos._reap(proc, time.monotonic()) is photos.NO_USAGE
    assert proc.returncode == 7


def good_record(data: bytes = PNG) -> bytes:
    """A record decoded in this process, its usage set to a child's (this process's own peak is
    the whole test worker's)."""
    return with_head(photo_decode.record(data), peak_kb=50_000, cpu_ms=200)


def with_head(record: bytes, **change: Any) -> bytes:
    n = int.from_bytes(record[:4])
    head = json.loads(record[4 : 4 + n]) | change
    head = {k: v for k, v in head.items() if v is not ...}
    text = json.dumps(head).encode()
    return struct.pack(">I", len(text)) + text + record[4 + n :]


def webp(im: Image.Image, **kw: Any) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "WEBP", quality=80, **kw)
    return buf.getvalue()


def with_blobs(record: bytes, new_thumb: bytes, new_detail: bytes, **change: Any) -> bytes:
    """record's header (changed) followed by these two variants instead of its own."""
    header = record[: 4 + int.from_bytes(record[:4])]
    return with_head(header, **change) + new_thumb + new_detail


def records() -> list[tuple[str, bytes]]:
    good = good_record()
    head = json.loads(good[4 : 4 + int.from_bytes(good[:4])])
    thumb_n, detail_n = head["thumb"], head["detail"]
    body = good[4 + int.from_bytes(good[:4]) :]
    thumb, detail = body[:thumb_n], body[thumb_n:]
    rgb = Image.new("RGB", (20, 10), "red")
    exif = Image.Exif()
    exif[0x0112] = 1
    with_exif = webp(rgb, exif=exif.tobytes())
    animated = webp(rgb, save_all=True, append_images=[Image.new("RGB", (20, 10))], duration=100)
    assert b"ANMF" in animated
    refusal = with_head(photo_decode.record(b"hello"), peak_kb=50_000, cpu_ms=200)
    return [
        ("empty", b""),
        ("short", b"\0\0"),
        ("header-too-long", struct.pack(">I", 2000) + b"{}" * 1000),
        ("not-json", struct.pack(">I", 3) + b"{x}"),
        ("extra-key", with_head(good, owner="x")),
        ("missing-key", with_head(good, cpu_ms=...)),
        ("lengths-off", good + b"\0"),
        ("code-not-the-childs", with_head(refusal, code="photo_too_large")),
        ("code-busy", with_head(refusal, code="busy")),
        ("format-gif", with_head(good, format="GIF")),
        ("width-0", with_head(good, width=0)),
        ("width-1201", with_head(good, width=1201)),
        ("pixels-over-cap", with_head(good, pixels=photo_decode.JPEG_CAP + 1)),
        ("size-not-the-detail", with_head(good, width=19)),
        ("error-with-newline", with_head(refusal, error="Value\nError")),
        ("error-not-a-known-name", with_head(refusal, error="S3cretExfil_AAAA")),
        ("error-a-class-not-raised", with_head(refusal, error="KeyError")),
        ("peak-past-twice-the-limit", with_head(good, peak_kb=2 * photo_child.MEMORY // 1024 + 1)),
        ("cpu-past-twice-the-limit", with_head(good, cpu_ms=2 * photo_child.CPU[1] * 1000 + 1)),
        ("refusal-with-bytes", refusal + b"\0"),
        ("thumb-not-webp", with_blobs(good, b"\0" * thumb_n, detail)),
        ("riff-size-wrong", with_blobs(good, thumb[:4] + b"\0\0\0\0" + thumb[8:], detail)),
        ("detail-with-exif", with_blobs(good, thumb, with_exif, detail=len(with_exif))),
        ("detail-animated", with_blobs(good, thumb, animated, detail=len(animated))),
        ("thumb-not-320", with_blobs(good, detail, detail, thumb=detail_n)),
    ]


def as_bytes(im: Image.Image, fmt: str, **kw: Any) -> bytes:
    buf = io.BytesIO()
    im.save(buf, fmt, **kw)
    return buf.getvalue()


def riff(chunks: list[tuple[bytes, bytes]], pad: bool = True) -> bytes:
    """A WebP from its chunks, its RIFF size its own (pad=False leaves an odd chunk unpadded)."""
    body = b"".join(
        kind + struct.pack("<I", len(data)) + data + (b"\0" * (len(data) & 1) if pad else b"")
        for kind, data in chunks
    )
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body


def chunks_of(blob: bytes) -> list[tuple[bytes, bytes]]:
    found, i = [], 12
    while i < len(blob):
        n = int.from_bytes(blob[i + 4 : i + 8], "little")
        found.append((blob[i : i + 4], blob[i + 8 : i + 8 + n]))
        i += 8 + n + (n & 1)
    return found


def webp_rows() -> list[tuple[str, bytes]]:
    """Variants a compromised child could write that only the structure check refuses (build
    review, correctness 6), each in a record otherwise good."""
    rgba = Image.new("RGBA", (20, 10), (200, 100, 50, 128))
    good = good_record(as_bytes(rgba, "PNG"))
    head = json.loads(good[4 : 4 + int.from_bytes(good[:4])])
    thumb = good[4 + int.from_bytes(good[:4]) :][: head["thumb"]]
    detail = good[4 + int.from_bytes(good[:4]) :][head["thumb"] :]
    vp8x, alph, vp8 = chunks_of(detail)
    assert (vp8x[0], alph[0], vp8[0]) == (b"VP8X", b"ALPH", b"VP8 ")
    rows = []
    for flag, name in ((0x20, "icc"), (0x08, "exif"), (0x04, "xmp"), (0x02, "animation")):
        flagged = riff([(b"VP8X", bytes([vp8x[1][0] | flag]) + vp8x[1][1:]), alph, vp8])
        rows.append((f"vp8x-{name}-flag", with_blobs(good, thumb, flagged, detail=len(flagged))))
    wider = vp8x[1][:4] + (20).to_bytes(3, "little") + vp8x[1][7:]  # canvas 21 x 10, frame 20 x 10
    canvas = riff([(b"VP8X", wider), alph, vp8])
    rows.append(("canvas-not-the-frame", with_blobs(good, thumb, canvas, detail=len(canvas))))
    odd = next(  # a lossless still (VP8L) whose one chunk has an odd length
        w
        for w in (
            as_bytes(Image.new("RGB", (20, 10), (n, 7, 9)), "WEBP", lossless=True)
            for n in range(256)
        )
        if int.from_bytes(w[16:20], "little") % 2
    )
    unpadded = riff(chunks_of(odd), pad=False)
    plain = good_record()
    plain_head = json.loads(plain[4 : 4 + int.from_bytes(plain[:4])])
    plain_thumb = plain[4 + int.from_bytes(plain[:4]) :][: plain_head["thumb"]]
    rows.append(
        ("odd-chunk-unpadded", with_blobs(plain, plain_thumb, unpadded, detail=len(unpadded)))
    )
    return rows


# ZIF-230 fence (build review, correctness 6). Kills: the structure check's VP8X flag rule, its
# canvas = frame rule, or its end-of-blob rule (an odd chunk must carry its pad byte) dropped.
@pytest.mark.parametrize(("name", "body"), webp_rows(), ids=[n for n, _ in webp_rows()])
def test_a_variant_outside_the_still_webp_layout_is_refused(name: str, body: bytes) -> None:
    assert photos.parse(body) is None


def test_the_rows_differ_from_a_good_rgba_record_only_where_named() -> None:
    rgba = Image.new("RGBA", (20, 10), (200, 100, 50, 128))
    assert photos.parse(good_record(as_bytes(rgba, "PNG"))) is not None


# ZIF-230 fence. Kills: trusting what the child wrote. Every part of the record is checked, and the
# variants (served publicly) must be plain still WebPs of the size the record names.
@pytest.mark.parametrize(("name", "body"), records(), ids=[n for n, _ in records()])
def test_a_malformed_record_is_refused(name: str, body: bytes) -> None:
    assert photos.parse(body) is None


# ZIF-230 fence (build review, security 7). Kills: the child's output copied on its way to the
# variants. The API holds one output buffer (OUTPUT_CAP) and one copy of the two variants, which
# the memory budget counts as 2 x OUTPUT_CAP.
def test_the_output_is_copied_once() -> None:
    noise_png = encoded(noise("RGBA", (1200, 1200), 0xFF, 9), "PNG")
    body = with_head(photo_decode.record(noise_png), peak_kb=50_000, cpu_ms=200)
    run = photos.Run(0, bytearray(photo_child.READY + body), None, 200)
    tracemalloc.start()
    try:
        outcome, _, thumb, detail = photos._judge(run)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert outcome == "ok" and len(thumb) + len(detail) > 2_000_000
    assert peak <= len(thumb) + len(detail) + 256 * 1024
    assert photos.OUTPUT_COPIES == 2


def test_a_good_record_parses() -> None:
    parsed = photos.parse(good_record())
    assert parsed is not None and isinstance(parsed[0], photos.Decoded)
    refusal = photos.parse(with_head(photo_decode.record(b"hello"), peak_kb=1, cpu_ms=1))
    assert refusal is not None and isinstance(refusal[0], photos.Refused)
    assert refusal[0].code == "photo_type"


# ZIF-230 guard: a malformed record from a real child run answers photo_unreadable.
def test_a_malformed_record_is_photo_unreadable(
    failed: Callable[[], list[dict[str, Any]]],
) -> None:
    assert refused(probe("probe-raw", with_head(good_record(), owner="x"))) == (
        422,
        "photo_unreadable",
    )
    assert [f["decoder"] for f in failed()] == ["protocol"]


# ZIF-230 fence. Kills: the API's environment (its secrets), a descriptor (a database socket) or
# the working directory reaching the child, -I/-B missing, or (Linux) a child still dumpable when it
# decodes. The probe decodes only if all hold.
def test_the_child_gets_no_environment_and_no_descriptors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZIF_S3_SECRET_ACCESS_KEY", "s3-" + uuid.uuid4().hex)
    read, write = os.pipe()
    os.set_inheritable(read, True)
    try:
        assert decode(probe("probe-inspect", PNG)).width == 20
    finally:
        os.close(read)
        os.close(write)


# ZIF-230 fence (ruling D3; build review, correctness 5, security 2). Kills: any syscall dropped
# from the filter's deny-list. A child that a decoder bug took over needs no network, so it gets
# none (nor io_uring, which would bypass the filter); it changes nothing on the filesystem: no
# directory, file, link, rename, truncation, permission, owner, time, extended attribute, hole or
# memfd (RLIMIT_FSIZE stops none of these), no open to write, create or truncate; and it runs no
# other program (an exec would make it dumpable again). Each call fails some other way without
# the filter.
@only_linux
def test_every_denied_syscall_is_refused() -> None:
    try:
        assert decode(probe("probe-deny", PNG)).width == 20
    except photos.Refusal as refusal:
        pytest.fail(f"check {refusal.fields.get('exit_code', 0) - 10} was not refused: {refusal}")


# ZIF-230 fence (build review, correctness 5). Kills: the filter's architecture check dropped. On
# x86_64 an x32 syscall kills the child (SIGSYS); on aarch64 the program starts by killing any
# other architecture's syscall.
@only_linux
def test_a_syscall_from_another_abi_is_killed(runs: list[photos.Run]) -> None:
    if os.uname().machine == "x86_64":
        refused(probe("probe-x32"))
        assert [r.returncode for r in runs] == [-signal.SIGSYS]
    else:
        head = struct.unpack("=HBBIHBBIHBBI", photo_child.program("aarch64", 1)[:24])
        assert head == (0x20, 0, 0, 4, 0x15, 1, 0, 0xC00000B7, 0x06, 0, 0, 0x80000000)


def own_state() -> dict[str, object]:
    me = os.getpid()
    if sys.platform != "linux":
        return {}
    return {
        "limits": {
            n: resource.getrlimit(getattr(resource, n)) for n in dir(resource) if n[:7] == "RLIMIT_"
        },
        "nice": os.getpriority(os.PRIO_PROCESS, me),
        "affinity": os.sched_getaffinity(me),
        "policy": os.sched_getscheduler(me),
    }


# ZIF-230 fence (build review, security 1). Kills: a child that a decoder bug took over killing or
# degrading the API by its pid: same-uid kill, prlimit64, setpriority, the scheduler calls and
# ioprio need no capability, and non-dumpability does not cover them. Each is refused unless it
# names the child itself; this process's limits, priority, affinity and policy stay as they were.
#
# Nor through the kernel's I/O signals (fcntl F_SETOWN/F_SETOWN_EX/F_SETSIG/F_SETLEASE/F_NOTIFY,
# F_SETFL with O_ASYNC, ioctl): the child cannot point its pipe or upload at the API, so no SIGIO
# (which ends a process by default) or chosen signal reaches it.
@only_linux
def test_the_child_cannot_reach_the_api_by_its_pid() -> None:
    received: list[int] = []
    watched = (signal.SIGIO, signal.SIGURG, signal.SIGTERM, signal.SIGUSR1)
    previous = {n: signal.signal(n, lambda number, frame: received.append(number)) for n in watched}
    before = own_state()
    try:
        assert decode(probe("probe-parent", PNG)).width == 20
        time.sleep(0.2)  # any signal the kernel would send
    except photos.Refusal as refusal:
        pytest.fail(f"check {refusal.fields.get('exit_code', 0) - 10} was not refused: {refusal}")
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
    assert received == []
    assert own_state() == before


# ZIF-230 fence (build review, security 1). Kills: the child in the API's process group, where a
# kill(0, ...) that any rule missed would reach the API too.
def test_the_child_has_a_process_group_of_its_own() -> None:
    assert decode(probe("probe-group", PNG)).width == 20


def status(pid: int | str) -> dict[str, str]:
    with open(f"/proc/{pid}/status") as lines:
        return {k: v.strip() for k, v in (line.split(":", 1) for line in lines)}


def proc_limits(pid: int) -> dict[str, tuple[str, str]]:
    with open(f"/proc/{pid}/limits") as limits:
        lines = limits.read().splitlines()[1:]
    return {line[:26].strip(): tuple(line[26:].split()[:2]) for line in lines}  # type: ignore[misc]


EXPECTED_LIMITS = {
    "Max address space": (str(photo_child.MEMORY), str(photo_child.MEMORY)),
    "Max cpu time": ("5", "6"),
    "Max file size": ("0", "0"),
    "Max open files": ("16", "16"),
    "Max processes": ("0", "0"),
    "Max core file size": ("0", "0"),
}


# ZIF-230 fence. Kills: limits or the syscall filter set after Pillow is imported, or not at all.
# The real child, started as photos starts it, blocks reading an upload that never comes: its
# limits and filter are in force and Pillow is not loaded yet.
@only_linux
def test_the_child_sets_its_limits_before_it_imports_pillow() -> None:
    read, write = os.pipe()
    with os.fdopen(read, "rb") as stdin:
        proc = photos._spawn(stdin)
    try:
        deadline = time.monotonic() + 10
        while proc_limits(proc.pid)["Max cpu time"] != ("5", "6"):
            assert time.monotonic() < deadline, proc_limits(proc.pid)
            time.sleep(0.01)
        time.sleep(0.2)  # past setrlimit, into the read
        limits = proc_limits(proc.pid)
        assert {k: limits[k] for k in EXPECTED_LIMITS} == EXPECTED_LIMITS
        with open(f"/proc/{proc.pid}/oom_score_adj") as adj:
            assert adj.read().strip() == "1000"
        # One filter more than this process has (the container runtime may have its own), and
        # no_new_privs, which a filter needs.
        theirs, ours = status(proc.pid), status("self")
        assert (theirs["NoNewPrivs"], theirs["Seccomp"]) == ("1", "2")
        assert int(theirs["Seccomp_filters"]) == int(ours["Seccomp_filters"]) + 1
        with open(f"/proc/{proc.pid}/maps") as maps:
            assert "PIL" not in maps.read()
    finally:
        os.close(write)
        proc.kill()
        proc.wait()
        assert proc.stdout is not None
        proc.stdout.close()


def noise(mode: str, size: tuple[int, int], mask: int, seed: int) -> Image.Image:
    """Pixels random in their low bits: costly to decode, still small enough to upload."""
    n = size[0] * size[1] * len(mode)
    table = bytes(i & mask for i in range(256))
    return Image.frombytes(mode, size, random.Random(seed).randbytes(n).translate(table))


def encoded(im: Image.Image, fmt: str, **kw: Any) -> bytes:
    buf = io.BytesIO()
    im.save(buf, fmt, **kw)
    return buf.getvalue()


def heaviest() -> Iterator[tuple[str, Callable[[], bytes]]]:
    """The costliest photos the caps let through, measured (design review, finding 2)."""
    yield "8 MP RGBA PNG", lambda: encoded(noise("RGBA", (3200, 2590), 0x01, 1), "PNG")
    yield "100 MP baseline JPEG", lambda: encoded(Image.new("RGB", (12500, 8000), "red"), "JPEG")
    yield "4 MP lossy RGBA WebP", lambda: encoded(noise("RGBA", (2000, 2000), 0x0F, 2), "WEBP")
    yield (
        "4 MP lossless WebP",
        lambda: encoded(noise("RGBA", (1999, 1999), 0x07, 3), "WEBP", lossless=True),
    )
    yield (
        "2-frame 4 MP WebP",
        lambda: encoded(
            noise("RGBA", (2000, 2000), 0x0F, 4),
            "WEBP",
            save_all=True,
            append_images=[noise("RGBA", (2000, 2000), 0x0F, 5)],
            quality=90,
        ),
    )
    yield (
        "8.3 MP CMYK progressive 4:4:4 JPEG",
        lambda: encoded(
            Image.new("CMYK", (3840, 2160), (10, 20, 30, 40)),
            "JPEG",
            progressive=True,
            subsampling=0,
        ),
    )
    yield (
        "8.3 MP RGB progressive 4:4:4 JPEG",
        lambda: encoded(
            noise("RGB", (3840, 2160), 0x07, 6), "JPEG", progressive=True, subsampling=0, quality=95
        ),
    )
    yield (
        "8.3 MP 16-bit grey PNG",
        lambda: encoded(
            Image.linear_gradient("L")
            .resize((3840, 2160))
            .convert("I")
            .point(lambda v: v * 257)
            .convert("I;16"),
            "PNG",
        ),
    )
    yield (
        "8.3 MP palette PNG with transparency",
        lambda: encoded(noise("RGB", (3840, 2160), 0x03, 7).convert("P"), "PNG", transparency=0),
    )
    yield (
        "1200 px RGBA noise (largest output)",
        lambda: encoded(noise("RGBA", (1200, 1200), 0xFF, 8), "PNG"),
    )


HEAVIEST = list(heaviest())


# ZIF-230 fence. Kills: limits so tight they refuse real photos, or headroom eroded unnoticed: each
# of the heaviest photos decodes in the real child with its peak under 80% of RLIMIT_AS and its
# CPU under 40% of the soft limit. The image's photo-check stage runs this on each architecture.
@pytest.mark.parametrize(("name", "make"), HEAVIEST, ids=[n for n, _ in HEAVIEST])
def test_the_heaviest_legitimate_photos_fit_the_limits(
    name: str, make: Callable[[], bytes]
) -> None:
    data = make()
    assert len(data) <= photos.MAX_BYTES, len(data)
    with tempfile.TemporaryFile() as upload_file:
        upload_file.write(data)
        upload_file.seek(0)
        run = photos._run(upload_file)
    outcome, head, _, _ = photos._judge(run)
    assert outcome == "ok", (outcome, run.returncode, head)
    assert head is not None
    print(f"{name}: peak {head.peak_kb / 1024:.0f} MiB, cpu {head.cpu_ms} ms")
    if LINUX:  # elsewhere the peak is resident memory, and no address-space limit applies
        assert head.peak_kb * 1024 <= 0.8 * photo_child.MEMORY
    assert head.cpu_ms <= 0.4 * photo_child.CPU[0] * 1000


def open_fds() -> int:
    return len(os.listdir("/proc/self/fd" if LINUX else "/dev/fd"))


# ZIF-230 fence. Kills: N uploads spawning N children (one per API process, under the lock), and
# a decode leaving its upload's temporary file or a pipe open.
def test_only_one_decoder_runs_at_a_time(s3: S3Client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(photos, "CHILD", PROBE)
    t = uuid.uuid4()
    ids = [upload(s3, t, probe("probe-nap:0.3", PNG)) for _ in range(3)]
    live, most, guard = 0, 0, threading.Lock()
    real = photos._run

    def counted(upload_file: Any) -> photos.Run:
        nonlocal live, most
        with guard:
            live += 1
            most = max(most, live)
        try:
            return real(upload_file)
        finally:
            with guard:
                live -= 1

    monkeypatch.setattr(photos, "_run", counted)
    before = open_fds()
    threads = [threading.Thread(target=photos.process, args=(t, i)) for i in ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert most == 1
    assert open_fds() == before


# ZIF-230 fence. Kills: a limit loosened without the review this pins, or the parent's copies of
# the child's numbers drifting from it.
def test_the_decoder_limits_are_pinned() -> None:
    mib = 1024 * 1024
    assert photo_child.LIMITS == {
        resource.RLIMIT_AS: (192 * mib, 192 * mib),
        resource.RLIMIT_CPU: (5, 6),
        resource.RLIMIT_FSIZE: (0, 0),
        resource.RLIMIT_NOFILE: (16, 16),
        resource.RLIMIT_NPROC: (0, 0),
        resource.RLIMIT_CORE: (0, 0),
    }
    assert (photos.DECODE_TIMEOUT, photos.STARVED_CPU_MS) == (10.0, 2500)
    assert photo_child.ALARM == 20 > photos.DECODE_TIMEOUT + photos.REAP_GRACE
    assert (photos.OUTPUT_CAP, photo_decode.ICC_CAP) == (4 * mib, mib)
    assert photos.MAX_PIXELS == photo_decode.JPEG_CAP
    assert photos.MAX_BYTES == photo_child.MAX_BYTES == 10 * mib


# ZIF-230 fence (build review, security 1). Kills: a syscall that names a process dropped from the
# filter, or a number drifting from the kernel's table (tests/photo_child_probe.py checks them
# against its own table on the architecture it runs on).
def test_the_filter_tables_are_pinned() -> None:
    assert photo_child.SELF == {
        "aarch64": {
            **{"kill": 129, "tkill": 130, "tgkill": 131},
            **{"rt_sigqueueinfo": 138, "rt_tgsigqueueinfo": 240},
        },
        "x86_64": {
            **{"kill": 62, "tkill": 200, "tgkill": 234},
            **{"rt_sigqueueinfo": 129, "rt_tgsigqueueinfo": 297},
        },
    }
    assert photo_child.SELF_OR_ZERO == {
        "aarch64": {
            **{"prlimit64": 261, "sched_setparam": 118, "sched_setscheduler": 119},
            **{"sched_setaffinity": 122, "sched_setattr": 274},
        },
        "x86_64": {
            **{"prlimit64": 302, "sched_setparam": 142, "sched_setscheduler": 144},
            **{"sched_setaffinity": 203, "sched_setattr": 314},
        },
    }
    named = {"setpriority", "ioprio_set", "pidfd_open", "pidfd_send_signal"}
    named |= {"process_vm_writev", "kcmp"}
    assert {k: photo_child.DENY["aarch64"][k] for k in named} == {
        **{"setpriority": 140, "ioprio_set": 30, "pidfd_open": 434, "pidfd_send_signal": 424},
        **{"process_vm_writev": 271, "kcmp": 272},
    }
    assert {k: photo_child.DENY["x86_64"][k] for k in named} == {
        **{"setpriority": 141, "ioprio_set": 251, "pidfd_open": 434, "pidfd_send_signal": 424},
        **{"process_vm_writev": 311, "kcmp": 312},
    }


def imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
    return found


# ZIF-230 fence. Kills: the child reaching the app's settings, database or store: its two modules
# import the standard library (and Pillow, to decode), nothing else.
def test_the_decoder_modules_import_nothing_from_the_app() -> None:
    app = API_DIR / "app"
    assert imports(app / "photo_child.py") <= {
        "ctypes",
        "errno",
        "importlib.util",
        "os",
        "platform",
        "resource",
        "signal",
        "struct",
        "sys",
        "pathlib",
        "types",
    }
    decoder = imports(app / "photo_decode.py")
    assert {m for m in decoder if m.split(".")[0] not in sys.stdlib_module_names} == {"PIL"}


# ZIF-230 fence. Kills: a deploy whose child cannot start telling owners their photo is unreadable
# (it is the server's fault: 503 busy, and an ERROR): any end before the ready marker, an exit or a
# signal (a filter for the wrong architecture, an import that crashes), since no upload byte has
# been parsed by then. And a decoder that, after its marker, fakes a start failure to page someone.
@pytest.mark.parametrize(
    ("child", "data", "answer", "outcome"),
    [
        (Path("/nonexistent/photo_child.py"), PNG, (503, "busy"), "import"),
        (PROBE, probe("probe-exit-before:3"), (503, "busy"), "limits"),
        (PROBE, probe("probe-exit-before:4"), (503, "busy"), "import"),
        (PROBE, probe("probe-exit-before:1"), (503, "busy"), "start"),
        (PROBE, probe("probe-crash-before"), (503, "busy"), "start"),
        (PROBE, probe("probe-exit-after:3"), (422, "photo_unreadable"), "exit"),
        (PROBE, probe("probe-exit-after:4"), (422, "photo_unreadable"), "exit"),
    ],
    ids=[
        "no-script",
        "limits",
        "import",
        "exit-1-before-ready",
        "crash-before-ready",
        "limits-after-ready",
        "import-after-ready",
    ],
)
def test_a_decoder_that_cannot_start_is_busy(
    child: Path,
    data: bytes,
    answer: tuple[int, str],
    outcome: str,
    failed: Callable[[], list[dict[str, Any]]],
) -> None:
    assert refused(data, child) == answer
    [line] = failed()
    assert line["decoder"] == outcome
    level = "photo decoder could not start" if answer[0] == 503 else "photo decoder failed"
    assert line["msg"] == level


def test_a_child_that_cannot_be_spawned_is_busy(
    monkeypatch: pytest.MonkeyPatch, failed: Callable[[], list[dict[str, Any]]]
) -> None:
    def no_pids(*args: object, **kw: object) -> None:
        raise BlockingIOError(11, "Resource temporarily unavailable")

    monkeypatch.setattr(subprocess, "Popen", no_pids)
    assert refused(PNG) == (503, "busy")
    assert [(f["msg"], f["decoder"]) for f in failed()] == [
        ("photo decoder could not start", "spawn")
    ]


# ZIF-230 fence. Kills: RLIMIT_NPROC missing (the child could fork or start threads, and its
# address space would grow by an arena per thread). Root is exempt from it (the dev image).
@pytest.mark.skipif(ROOT, reason="RLIMIT_NPROC is not enforced for root (dev image)")
def test_the_child_cannot_fork_or_start_threads() -> None:
    # macOS does not count threads against RLIMIT_NPROC; Linux does (the probe checks both there).
    assert decode(probe("probe-fork", PNG)).width == 20


MIB = 1024 * 1024


@pytest.fixture
def cgroup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake /sys/fs/cgroup for this process's container."""
    monkeypatch.setattr(photos, "CGROUP", tmp_path)
    return tmp_path


# ZIF-230 fence (review finding 1). Kills: a pod whose memory limit cannot hold the API and a child
# at its ceiling going unnoticed. With memory.oom.group (the kubelet sets it on cgroup v2) a child
# OOM restarts the whole pod, so the budget is checked at startup and logged, never refused.
@pytest.mark.parametrize(
    ("memory_max", "state"),
    [(None, "unlimited"), ("max", "unlimited"), (str(384 * MIB), "ok"), (str(300 * MIB), "tight")],
)
def test_the_startup_budget_check(
    cgroup: Path,
    memory_max: str | None,
    state: str,
    failed: Callable[[], list[dict[str, Any]]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    if memory_max is not None:
        (cgroup / "memory.max").write_text(memory_max + "\n")
        (cgroup / "memory.oom.group").write_text("1\n")
    with caplog.at_level(logging.ERROR, logger="app.photos"):
        assert photos.budget() == state
    errors = [r.__dict__ for r in caplog.records if r.getMessage().startswith("photo decoder mem")]
    if state == "tight":
        [line] = errors
        assert line["memory_max"] == 300 * MIB and line["oom_group"] == 1
        assert line["required"] == (
            photos.API_ALLOWANCE
            + photo_child.MEMORY
            + photos.OUTPUT_COPIES * photos.OUTPUT_CAP
            + photos.BUDGET_MARGIN
        )
    else:
        assert errors == []


# ZIF-230 fence (review finding 1). Kills: a spawn the pod's memory cannot hold, which with
# memory.oom.group would restart the pod: it answers 503 busy instead and starts nothing.
def test_a_decode_the_pod_cannot_hold_is_busy(
    cgroup: Path,
    monkeypatch: pytest.MonkeyPatch,
    runs: list[photos.Run],
    failed: Callable[[], list[dict[str, Any]]],
) -> None:
    (cgroup / "memory.max").write_text(str(384 * MIB))
    monkeypatch.setattr(photos, "_rss", lambda: 200 * MIB)
    assert refused(PNG, photos.CHILD) == (503, "busy")
    assert runs == []
    [line] = failed()
    assert (line["msg"], line["decoder"]) == ("photo decoder could not start", "headroom")
    assert (line["memory_max"], line["rss"]) == (384 * MIB, 200 * MIB)
    monkeypatch.setattr(photos, "_rss", lambda: 150 * MIB)
    assert decode(PNG, photos.CHILD).width == 20


def test_the_own_rss_is_read() -> None:
    rss = photos._rss()
    assert (rss > 10 * MIB) if LINUX else rss == 0


# ZIF-230 fence (review finding 1). Kills: a limit raised past what the pods were sized for. The
# production and staging API pods have 384Mi (cloud-infra clusters/hetzner/ziftbook-*/backend.yaml);
# a child OOM there restarts the pod, so raising the child's limit or the API's allowance means
# raising that limit with it.
def test_the_memory_budget_fits_the_pod() -> None:
    child = photo_child.MEMORY + photos.OUTPUT_COPIES * photos.OUTPUT_CAP
    assert photos.API_ALLOWANCE + child + photos.BUDGET_MARGIN <= 384 * MIB


HARDENED = """
import ctypes, sys, tempfile, uuid
from fastapi.testclient import TestClient
from opentelemetry import trace
from app import photos
from app.main import create_app
with TestClient(create_app()):
    pass
dumpable = ctypes.CDLL(None).prctl(3, 0, 0, 0, 0)
photos.CHILD = photos.Path(sys.argv[1])
with tempfile.TemporaryFile() as upload:
    upload.write(b"probe-environ\\n" + open(sys.argv[2], "rb").read())
    upload.seek(0)
    try:
        photos.variants(upload, uuid.uuid4(), trace.INVALID_SPAN)
        print("RESULT", dumpable, "refused")
    except photos.Refusal as refusal:
        print("RESULT", dumpable, refusal.fields)
"""


# ZIF-230 fence (ruling D1). Kills: env={} bypassed through /proc. A child taken over by a decoder
# bug runs as the API's uid and could read the API's environment (its secrets) from
# /proc/<pid>/environ unless the API is not dumpable. The real app's startup must set that.
@only_linux
def test_a_child_cannot_read_the_api_environment(tmp_path: Path) -> None:
    png = tmp_path / "photo.png"
    png.write_bytes(PNG)
    run = subprocess.run(
        [sys.executable, "-c", HARDENED, str(PROBE), str(png)],
        cwd=API_DIR,
        capture_output=True,
        text=True,
        env=os.environ,
    )
    [result] = [line for line in run.stdout.splitlines() if line.startswith("RESULT")]
    assert result == "RESULT 0 refused", run.stderr[-2000:]


# ZIF-230 guard (ruling D1): a failed hardening is an ERROR at startup, never a refusal to start.
def test_a_failed_hardening_is_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(photos, "_undumpable", lambda: False)
    assert photos.harden("linux") == "failed"
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR] == [
        "api hardening failed: photo decodes refused"
    ]
    monkeypatch.setattr(photos, "_undumpable", lambda: True)
    assert photos.harden("linux") == "on"
    assert photos.harden("darwin") == "unsupported"


def set_dumpable(value: int) -> None:
    import ctypes

    assert ctypes.CDLL(None).prctl(4, value, 0, 0, 0) == 0  # PR_SET_DUMPABLE


# ZIF-230 fence (build review, security 3). Kills: a decode while this process is dumpable, however
# it got there (hardening never run, as with --lifespan off or a script, or failed): its child
# could read this process's environment through /proc. The invariant itself is checked before
# every spawn, not the bookkeeping: 503 busy and an ERROR, no child.
@only_linux
def test_decodes_are_refused_while_the_api_is_dumpable(
    runs: list[photos.Run], failed: Callable[[], list[dict[str, Any]]]
) -> None:
    set_dumpable(1)
    try:
        assert refused(PNG, photos.CHILD) == (503, "busy")
    finally:
        set_dumpable(0)
    assert runs == []
    assert [(f["msg"], f["decoder"]) for f in failed()] == [
        ("photo decoder could not start", "hardening")
    ]
    assert decode(PNG, photos.CHILD).width == 20


# ZIF-230 fence (review finding 11). Kills: decoder failures noticed only by someone reading logs:
# every decode counts once under its outcome, a closed set, so an alert can be built on it.
def test_every_decode_is_counted_by_outcome(
    metric_reader: InMemoryMetricReader, monkeypatch: pytest.MonkeyPatch
) -> None:
    decode(PNG, photos.CHILD)
    with pytest.raises(ApiError):
        decode(b"hello", photos.CHILD)
    with pytest.raises(ApiError):
        decode(probe("probe-crash"))
    monkeypatch.setattr(photos, "_api_dumpable", lambda: True)
    with pytest.raises(ApiError):
        decode(PNG, photos.CHILD)
    counted = {
        p.attributes["photo.outcome"]: p.value
        for p in points(metric_reader, "ziftbook.photo.decodes")
    }
    assert counted == {"ok": 1, "refused": 1, "signal": 1, "hardening": 1}
    assert set(photos.OUTCOMES) == {
        *("ok", "refused", "cpu", "memory", "oom", "signal", "exit", "output", "protocol"),
        *("timeout", "starved", "headroom", "scratch", "spawn", "limits", "import", "start"),
        "hardening",
    }
    closed(metric_reader)


class FullDisk(io.RawIOBase):
    """A file on a full disk: every write to it fails with ENOSPC."""

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = 0) -> int:
        return 0

    def readinto(self, buffer: Any) -> int:
        return 0

    def write(self, data: Any) -> int:
        raise OSError(28, "No space left on device")


# ZIF-230 fence (build review, correctness 3). Kills: a full or unwritable /tmp (the upload's
# scratch file) answering 500: it is the server's fault, 503 busy and an ERROR, and no child starts.
# Through the real buffered write path: a small upload fails only when its buffer is flushed (at
# the seek before the decode), a large one while it streams.
@pytest.mark.parametrize(
    ("where", "size"), [("create", 10), ("flush", 100_000), ("write", 600_000)]
)
def test_no_scratch_space_for_the_upload_is_busy(
    where: str,
    size: int,
    s3: S3Client,
    monkeypatch: pytest.MonkeyPatch,
    runs: list[photos.Run],
    failed: Callable[[], list[dict[str, Any]]],
) -> None:
    t = uuid.uuid4()
    upload_id = upload(s3, t, b"\0" * size)

    def scratch() -> io.BufferedRandom:
        if where == "create":
            raise OSError(28, "No space left on device")
        return io.BufferedRandom(FullDisk())

    monkeypatch.setattr(tempfile, "TemporaryFile", scratch)
    with pytest.raises(ApiError) as error:
        photos.process(t, upload_id)
    assert (error.value.status_code, error.value.code) == (503, "busy")
    assert runs == []
    assert [(f["msg"], f["decoder"]) for f in failed()] == [
        ("photo decoder could not start", "scratch")
    ]


def run_filter(machine: str, nr: int, args: tuple[int, ...] = (), arch: int | None = None) -> int:
    """What photo_child's filter for machine (pid 4242) answers a syscall: classic BPF, run here."""
    prog = photo_child.program(machine, 4242)
    full = (*args, *(0,) * (6 - len(args)))
    data = struct.pack("=iIQ6Q", nr, arch or photo_child.AUDIT_ARCH[machine], 0, *full)
    pc, acc = 0, 0
    while True:
        code, jt, jf, k = (int(v) for v in struct.unpack_from("=HBBI", prog, pc * 8))
        pc += 1
        if code == 0x20:
            acc = struct.unpack_from("=I", data, k)[0]
        elif code == 0x06:
            return k
        else:
            taken = {0x15: acc == k, 0x35: acc >= k, 0x45: bool(acc & k)}[code]
            pc += jt if taken else jf


ALLOW, KILL, EPERM = 0x7FFF0000, 0x80000000, 0x00050001
MACHINES = ["aarch64", "x86_64"]


# ZIF-230 fence (build review, correctness 5). Kills: a syscall dropped from either architecture's
# deny-list (io_uring included, which a container runtime may refuse on its own, hiding a gap), a
# pid rule loosened, the write-flag check or the architecture checks gone. The program itself is
# run here for both architectures against this file's own syscall table (tests/photo_syscalls.py).
@pytest.mark.parametrize("machine", MACHINES)
def test_the_filter_program_decides_each_syscall(machine: str) -> None:
    i = MACHINES.index(machine)
    for name, numbers in photo_syscalls.DENIED.items():
        number = numbers[i]
        if number is not None:
            assert run_filter(machine, number) == EPERM, name
    nr = {name: numbers[i] for name, numbers in photo_syscalls.NR.items()}
    assert run_filter(machine, nr["read"]) == ALLOW
    assert run_filter(machine, nr["kill"], (4242, 0)) == ALLOW
    assert run_filter(machine, nr["kill"], (1, 9)) == EPERM
    assert run_filter(machine, nr["kill"], (0, 9)) == EPERM
    assert run_filter(machine, nr["kill"], (0xFFFFFFFF, 9)) == EPERM
    assert run_filter(machine, nr["tgkill"], (4242, 4242, 6)) == ALLOW
    assert run_filter(machine, nr["prlimit64"], (0,)) == ALLOW
    assert run_filter(machine, nr["prlimit64"], (4242,)) == ALLOW
    assert run_filter(machine, nr["prlimit64"], (1,)) == EPERM
    assert run_filter(machine, nr["openat"], (0, 0, os.O_RDONLY)) == ALLOW
    for flag in (0o1, 0o2, 0o100, 0o1000):  # O_WRONLY, O_RDWR, O_CREAT, O_TRUNC on Linux
        assert run_filter(machine, nr["openat"], (0, 0, flag)) == EPERM
    # fcntl: the commands that point I/O signals at a process, and async mode, are refused.
    for cmd in (8, 15, 10, 1024, 1026):  # F_SETOWN, F_SETOWN_EX, F_SETSIG, F_SETLEASE, F_NOTIFY
        assert run_filter(machine, nr["fcntl"], (1, cmd, 4242)) == EPERM, cmd
    assert run_filter(machine, nr["fcntl"], (1, 4, 0o20000)) == EPERM  # F_SETFL O_ASYNC
    assert run_filter(machine, nr["fcntl"], (1, 4, 0o4000)) == ALLOW  # F_SETFL O_NONBLOCK
    for cmd in (0, 1, 2, 3, 9, 1030):  # F_DUPFD, F_GETFD, F_SETFD, F_GETFL, F_GETOWN, CLOEXEC
        assert run_filter(machine, nr["fcntl"], (1, cmd, 0o20000)) == ALLOW, cmd
    # Past the highest number the tables were written against (a syscall a later kernel adds):
    # "no such syscall", so a library falls back as it would on an older kernel.
    enosys = 0x00050000 | 38
    for number in (470, 471, 1000, 0x3FFFFFFF):
        assert run_filter(machine, number) == enosys, number
    assert run_filter(machine, photo_child.CEILING) != enosys
    other = photo_child.AUDIT_ARCH["x86_64" if machine == "aarch64" else "aarch64"]
    assert run_filter(machine, nr["read"], arch=other) == KILL
    assert run_filter(machine, nr["read"], arch=0x40000003) == KILL  # i386
    if machine == "x86_64":
        assert run_filter(machine, 0x40000000 | 39) == KILL  # x32


# ZIF-230 fence (build review, security 5). Kills: a free-text channel from the child into the logs:
# `error` is a closed set (the checks' names and the classes the decode catches), the same on both
# sides, and an exception is named by the first of them in its class's ancestry.
def test_the_error_names_are_a_closed_set() -> None:
    assert photos.ERRORS == photo_decode.ERRORS
    assert set(photos.ERRORS) == {
        *("unsafe_container", "pixel_cap", "exif_cap", "icc_cap"),
        *("UnidentifiedImageError", "DecompressionBombError", "OSError", "ValueError"),
        *("SyntaxError", "TypeError", "AttributeError", "error"),
    }
    assert photo_decode.error_name(UnicodeDecodeError("utf-8", b"", 0, 1, "x")) == "ValueError"
    assert photo_decode.error_name(FileNotFoundError()) == "OSError"
    assert photo_decode.error_name(struct.error()) == "error"


# ZIF-230 fence (build review, correctness 10a). Kills: a typo minting a new value of the metric's
# closed attribute: an outcome outside OUTCOMES is refused where it is counted.
def test_only_a_known_outcome_is_counted() -> None:
    with pytest.raises(ValueError):
        photos._count("timeuot", trace.INVALID_SPAN)  # type: ignore[arg-type]
