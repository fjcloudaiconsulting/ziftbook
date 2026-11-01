"""A stand-in for app/photo_child.py in tests (ZIF-230), run by photos.py exactly as the real child
is (tests point photos.CHILD here). It loads the real photo_child.py by path and runs its main(),
so the real limits, upload read, ready marker and exit codes apply; only the decoder is swapped
for one that misbehaves as the upload's first line says. The child has no environment and no
arguments, so the upload is the only way to tell it what to do."""

import ctypes
import importlib.util
import json
import os
import resource
import signal
import socket
import struct
import sys
import threading
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

APP = Path(__file__).resolve().parent.parent / "app"


def load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


child = load("photo_child", APP / "photo_child.py")
real_load = child.load
# stdin is the upload's file: read its first line without moving the offset main() reads from.
MODE, _, ARG = os.pread(0, 256, 0).partition(b"\n")[0].decode().partition(":")


def refusal(error: str) -> bytes:
    """A well-formed refusal record carrying error: how a probe reports what it saw."""
    head: dict[str, Any] = {"kind": "refused", "code": "photo_unreadable", "error": error}
    head |= {"format": None, "pixels": None, "peak_kb": 0, "cpu_ms": 0}
    text = json.dumps(head).encode()
    return struct.pack(">I", len(text)) + text


# The syscalls a probe calls by number, its own table (not photo_child's): (aarch64, x86_64).
NR = {
    "tkill": (130, 200),
    "tgkill": (131, 234),
    "rt_sigqueueinfo": (138, 129),
    "rt_tgsigqueueinfo": (240, 297),
    "sched_setattr": (274, 314),
    "ioprio_get": (31, 252),
    "ioprio_set": (30, 251),
    "process_vm_writev": (271, 311),
    "kcmp": (272, 312),
}


def syscall(name: str, *args: Any) -> int:
    nr = NR[name][0 if os.uname().machine == "aarch64" else 1]
    libc = ctypes.CDLL(None, use_errno=True)
    result: int = libc.syscall(nr, *[ctypes.c_long(a) if isinstance(a, int) else a for a in args])
    if result == -1:
        raise OSError(ctypes.get_errno(), name)
    return result


def expect(checks: list[tuple[Any, int | None]]) -> None:
    """Exit 10 + i at the first check that does not end as it should: an errno, or None to
    succeed. The decode's exit code then names the check."""
    for i, (call, wanted) in enumerate(checks):
        try:
            call()
            got = None
        except OSError as error:
            got = error.errno
        if got != wanted:
            os._exit(10 + i)


def siginfo() -> Any:
    """A siginfo for sig 0 with si_code SI_QUEUE (-1), as a user process may send."""
    info = ctypes.create_string_buffer(128)
    struct.pack_into("=iii", info, 0, 0, 0, -1)
    return info


def parent_checks() -> list[tuple[Any, int | None]]:
    """Every call that names another process, against the API (its parent), each a no-op if it
    were allowed; then the same calls against this process, which must stay allowed."""
    if sys.platform != "linux":  # the filter is Linux's; so are half of these calls
        return []
    api, me, eperm = os.getppid(), os.getpid(), 1
    sched_attr = ctypes.create_string_buffer(48)
    struct.pack_into("=I", sched_attr, 0, 48)
    return [
        (lambda: os.kill(api, 0), eperm),
        (lambda: os.kill(0, 0), eperm),  # the process group
        (lambda: os.kill(-1, 0), eperm),  # every process it may signal
        (lambda: syscall("tkill", api, 0), eperm),
        (lambda: syscall("tgkill", api, api, 0), eperm),
        (lambda: syscall("rt_sigqueueinfo", api, 0, siginfo()), eperm),
        (lambda: syscall("rt_tgsigqueueinfo", api, api, 0, siginfo()), eperm),
        (lambda: resource.prlimit(api, resource.RLIMIT_MSGQUEUE), eperm),
        (lambda: os.setpriority(os.PRIO_PROCESS, api, os.getpriority(os.PRIO_PROCESS, api)), eperm),
        (lambda: os.sched_setaffinity(api, os.sched_getaffinity(api)), eperm),
        (lambda: os.sched_setscheduler(api, os.SCHED_OTHER, os.sched_param(0)), eperm),
        (lambda: os.sched_setparam(api, os.sched_param(0)), eperm),
        (lambda: syscall("sched_setattr", api, sched_attr, 0), eperm),
        (lambda: syscall("ioprio_set", 1, api, syscall("ioprio_get", 1, api)), eperm),
        (lambda: os.pidfd_open(api), eperm),
        (lambda: signal.pidfd_send_signal(0, 0), eperm),
        (lambda: syscall("process_vm_writev", me, None, 0, None, 0, 0), eperm),
        (lambda: syscall("kcmp", me, me, 0, 0, 0), eperm),
        (lambda: os.setpriority(os.PRIO_PROCESS, 0, os.getpriority(os.PRIO_PROCESS, 0)), eperm),
        # Still allowed: the same calls on itself (glibc's raise and setrlimit use them).
        (lambda: os.kill(me, 0), None),
        (lambda: syscall("tgkill", me, me, 0), None),
        (lambda: resource.prlimit(0, resource.RLIMIT_NOFILE), None),
        (lambda: resource.prlimit(me, resource.RLIMIT_NOFILE), None),
        (lambda: os.sched_setaffinity(0, os.sched_getaffinity(0)), None),
        (lambda: os.sched_setscheduler(me, os.SCHED_OTHER, os.sched_param(0)), None),
    ]


def spin() -> None:
    while True:
        pass


def inspect(rest: bytes) -> bytes:
    """The real decode, only if this process got nothing it should not have."""
    environ = set(os.environ) - {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}  # set by Python, macOS
    listing = "/proc/self/fd" if sys.platform == "linux" else "/dev/fd"
    fds = set()
    for name in os.listdir(listing):
        try:
            os.fstat(int(name))
            fds.add(int(name))
        except OSError:
            pass  # the listing's own descriptor, closed by now
    if environ or fds != {0, 1, 2} or not sys.flags.isolated:
        os._exit(9)
    if not sys.flags.dont_write_bytecode or os.getcwd() != "/":
        os._exit(9)
    # Not dumpable by the time it decodes: no core file of the upload, whatever the host's
    # core_pattern (a piped one ignores RLIMIT_CORE).
    if sys.platform == "linux" and ctypes.CDLL(None).prctl(3, 0, 0, 0, 0) != 0:  # PR_GET_DUMPABLE
        os._exit(9)
    data: bytes = real_load().record(rest)
    return data


def record(data: bytes) -> bytes:
    rest = data.partition(b"\n")[2]
    if MODE == "probe-raw":  # exactly the bytes after the first line
        return rest
    if MODE == "probe-inspect":
        return inspect(rest)
    if MODE == "probe-nap":  # a real decode that takes a while
        time.sleep(float(ARG))
        found: bytes = real_load().record(rest)
        return found
    if MODE == "probe-memory":  # a Python allocation past the address-space limit
        return refusal(f"allocated_{len(bytearray(1 << 30))}")
    if MODE == "probe-native-memory":  # Pillow's own allocation past it
        from PIL import Image

        Image.new("RGBA", (12_000, 12_000))
        return refusal("allocated")
    if MODE == "probe-near-limit":  # a refusal from a child that came close to the limit
        decoder, ballast = real_load(), list[bytearray]()
        try:
            while True:
                ballast.append(bytearray(4 * 1024 * 1024))
        except MemoryError:
            del ballast[-4:]
        found = decoder.record(b"not an image")
        return found
    if MODE == "probe-crash":
        ctypes.string_at(0)
    if MODE == "probe-cpu":  # past a CPU limit lowered to 1 s
        resource.setrlimit(resource.RLIMIT_CPU, (1, child.CPU[1]))
        spin()
    if MODE == "probe-spin":  # busy past the parent's deadline
        spin()
    if MODE == "probe-sleep":
        time.sleep(60)
    if MODE == "probe-close-then-sleep":
        sys.stdout.flush()
        os.close(1)
        time.sleep(60)
    if MODE == "probe-close-then-spin":
        sys.stdout.flush()
        os.close(1)
        spin()
    if MODE == "probe-overflow-exit":  # one byte past the cap, then gone
        sys.stdout.buffer.write(b"\0" * (int(ARG) + 1))
        sys.stdout.flush()
        os._exit(0)
    if MODE == "probe-flood":
        while True:
            sys.stdout.buffer.write(b"\0" * 65_536)
    if MODE == "probe-exit-after":
        sys.stdout.flush()
        os._exit(int(ARG))
    if MODE == "probe-fork":
        try:
            pid = os.fork()
            if pid == 0:
                os._exit(0)
            os.waitpid(pid, 0)
            forked = "yes"
        except OSError:
            forked = "no"
        try:
            thread = threading.Thread(target=lambda: None)
            thread.start()
            thread.join()
            threaded = "yes"
        except RuntimeError:
            threaded = "no"
        return refusal(f"fork_{forked}_thread_{threaded}")
    if MODE == "probe-socket":
        try:
            socket.socket(socket.AF_INET, socket.SOCK_STREAM).close()
            return refusal("allowed")
        except OSError as error:
            return refusal(type(error).__name__)
    if MODE == "probe-write":  # a directory, a new file, the upload opened for writing
        seen = []
        for attempt in (
            lambda: os.mkdir(f"/tmp/zif230-probe-{os.getpid()}"),
            lambda: os.open(f"/tmp/zif230-probe-{os.getpid()}.f", os.O_WRONLY | os.O_CREAT),
            lambda: os.open("/proc/self/fd/0", os.O_RDWR),
        ):
            try:
                attempt()
                seen.append("allowed")
            except OSError as error:
                seen.append(type(error).__name__)
        return refusal("_".join(seen))
    if MODE == "probe-group":  # a process group of its own, apart from the API's
        if os.getpgrp() != os.getpid():
            os._exit(9)
        grouped: bytes = real_load().record(rest)
        return grouped
    if MODE == "probe-parent":  # nothing reaches the API by its pid
        expect(parent_checks())
        checked: bytes = real_load().record(rest)
        return checked
    if MODE == "probe-environ":  # the API's own environment, through /proc
        try:
            with open(f"/proc/{os.getppid()}/environ", "rb") as environ:
                environ.read()
            return refusal("allowed")
        except OSError as error:
            return refusal(type(error).__name__)
    raise SystemExit(f"unknown probe {MODE}")


def fake_load() -> Any:
    if MODE == "probe-exit-before":
        os._exit(int(ARG))
    return SimpleNamespace(record=record)


child.load = fake_load  # type: ignore[attr-defined]
sys.exit(child.main())
