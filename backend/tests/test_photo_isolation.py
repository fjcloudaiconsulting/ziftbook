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
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from mypy_boto3_s3 import S3Client
from opentelemetry import trace
from PIL import Image

from app import photo_child, photo_decode, photos
from app.errors import ApiError
from tests.conftest import API_DIR, image, upload

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


def probe_says(mode: str) -> str:
    """What a probe that reports through its refusal record saw."""
    with pytest.raises(photos.Refusal) as error:
        decode(probe(mode))
    assert error.value.fields["decoder"] == "refused"
    said: str = error.value.fields["error"]
    return said


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


# ZIF-230 fence. Kills: the child's output read without a cap into the API's memory.
def test_decoder_output_is_capped(
    runs: list[photos.Run], failed: Callable[[], list[dict[str, Any]]]
) -> None:
    assert refused(probe("probe-flood")) == (422, "photo_unreadable")
    [run] = runs
    assert (run.killed, run.returncode) == ("output", -signal.SIGKILL)
    assert len(run.output) <= len(photo_child.READY) + photos.OUTPUT_CAP + photos.CHUNK
    assert [f["decoder"] for f in failed()] == ["output"]


def good_record(data: bytes = PNG) -> bytes:
    return photo_decode.record(data)


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
    refusal = photo_decode.record(b"hello")
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
        ("refusal-with-bytes", refusal + b"\0"),
        ("thumb-not-webp", with_blobs(good, b"\0" * thumb_n, detail)),
        ("riff-size-wrong", with_blobs(good, thumb[:4] + b"\0\0\0\0" + thumb[8:], detail)),
        ("detail-with-exif", with_blobs(good, thumb, with_exif, detail=len(with_exif))),
        ("detail-animated", with_blobs(good, thumb, animated, detail=len(animated))),
        ("thumb-not-320", with_blobs(good, detail, detail, thumb=detail_n)),
    ]


# ZIF-230 fence. Kills: trusting what the child wrote. Every part of the record is checked, and the
# variants (served publicly) must be plain still WebPs of the size the record names.
@pytest.mark.parametrize(("name", "body"), records(), ids=[n for n, _ in records()])
def test_a_malformed_record_is_refused(name: str, body: bytes) -> None:
    assert photos.parse(body) is None


def test_a_good_record_parses() -> None:
    parsed = photos.parse(good_record())
    assert parsed is not None and isinstance(parsed[0], photos.Decoded)
    refusal = photos.parse(photo_decode.record(b"hello"))
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


# ZIF-230 fence (ruling D3). Kills: a child that a decoder bug took over reaching the network (egress
# is open to the cluster and the internet) or io_uring, which would bypass the filter.
@only_linux
def test_the_child_cannot_open_a_socket() -> None:
    assert probe_says("probe-socket") == "PermissionError"


# ZIF-230 fence (ruling D3). Kills: a child that a decoder bug took over changing the filesystem: a
# directory or an empty file in /tmp (RLIMIT_FSIZE stops neither), or the upload opened to write.
@only_linux
def test_the_child_cannot_change_the_filesystem() -> None:
    assert probe_says("probe-write") == "PermissionError_PermissionError_PermissionError"


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
    assert (photos.OUTPUT_CAP, photo_decode.ICC_CAP) == (4 * mib, mib)
    assert photos.MAX_PIXELS == photo_decode.JPEG_CAP
    assert photos.MAX_BYTES == photo_child.MAX_BYTES == 10 * mib


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
        "struct",
        "sys",
        "pathlib",
        "types",
    }
    decoder = imports(app / "photo_decode.py")
    assert {m for m in decoder if m.split(".")[0] not in sys.stdlib_module_names} == {"PIL"}


# ZIF-230 fence. Kills: a deploy whose child cannot start telling owners their photo is unreadable
# (it is the server's fault: 503 busy, and an ERROR), and a decoder that, after its ready marker,
# fakes a start failure to page someone.
@pytest.mark.parametrize(
    ("child", "data", "answer", "outcome"),
    [
        (Path("/nonexistent/photo_child.py"), PNG, (503, "busy"), "import"),
        (PROBE, probe("probe-exit-before:3"), (503, "busy"), "limits"),
        (PROBE, probe("probe-exit-before:4"), (503, "busy"), "import"),
        (PROBE, probe("probe-exit-after:3"), (422, "photo_unreadable"), "exit"),
        (PROBE, probe("probe-exit-after:4"), (422, "photo_unreadable"), "exit"),
    ],
    ids=["no-script", "limits", "import", "limits-after-ready", "import-after-ready"],
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
    # macOS does not count threads against RLIMIT_NPROC; Linux does.
    assert probe_says("probe-fork") == ("fork_no_thread_no" if LINUX else "fork_no_thread_yes")
