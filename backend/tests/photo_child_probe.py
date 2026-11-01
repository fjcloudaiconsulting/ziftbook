"""A stand-in for app/photo_child.py in tests (ZIF-230), run by photos.py exactly as the real child
is (tests point photos.CHILD here). It loads the real photo_child.py by path and runs its main(),
so the real limits, upload read, ready marker and exit codes apply; only the decoder is swapped
for one that misbehaves as the upload's first line says. The child has no environment and no
arguments, so the upload is the only way to tell it what to do."""

import ctypes
import fcntl
import importlib.util
import json
import mmap
import os
import resource
import signal
import socket
import struct
import sys
import termios
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
tables = load("photo_syscalls", Path(__file__).with_name("photo_syscalls.py"))
NR, DENIED, DENIED_ARGS = tables.NR, tables.DENIED, tables.DENIED_ARGS
real_load = child.load
# stdin is the upload's file: read its first line without moving the offset main() reads from.
MODE, _, ARG = os.pread(0, 256, 0).partition(b"\n")[0].decode().partition(":")


def refusal(error: str) -> bytes:
    """A well-formed refusal record carrying error: how a probe reports what it saw."""
    head: dict[str, Any] = {"kind": "refused", "code": "photo_unreadable", "error": error}
    head |= {"format": None, "pixels": None, "peak_kb": 0, "cpu_ms": 0}
    text = json.dumps(head).encode()
    return struct.pack(">I", len(text)) + text


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


def raw(nr: int, *args: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.syscall(nr, *[ctypes.c_long(a) for a in args]) == -1:
        raise OSError(ctypes.get_errno(), str(nr))


def deny_checks() -> list[tuple[Any, int | None]]:
    if sys.platform != "linux":
        return []
    arm = os.uname().machine == "aarch64"
    eperm, new = 1, f"/tmp/zif230-probe-{os.getpid()}"
    checks: list[tuple[Any, int | None]] = []
    for name, numbers in DENIED.items():
        nr = numbers[0 if arm else 1]
        if nr is not None:
            args = DENIED_ARGS.get(name, (0,) * 6)
            checks.append((lambda nr=nr, args=args: raw(nr, *args), eperm))
    return checks + [
        (lambda: socket.socket(socket.AF_INET6, socket.SOCK_DGRAM), eperm),
        (lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM), eperm),
        (lambda: os.open(new, os.O_WRONLY | os.O_CREAT), eperm),
        (lambda: os.open(new, os.O_RDONLY | os.O_CREAT), eperm),
        (lambda: os.open("/proc/self/fd/0", os.O_RDWR), eperm),
        (lambda: os.open("/proc/self/fd/0", os.O_RDONLY | os.O_TRUNC), eperm),
        (lambda: os.close(os.open("/proc/self/fd/0", os.O_RDONLY)), None),  # reading stays allowed
    ]


# fcntl commands the module does not name on every platform (Linux values).
F_SETOWN_EX, F_SETSIG, F_SETLEASE, F_NOTIFY = 15, 10, 1024, 1026
FIOSETOWN, SIOCSPGRP = 0x8901, 0x8902  # nor these ioctls


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
        # I/O signals sent to the API by the kernel: an owner for its pipe or upload, a signal for
        # it, async mode, a lease or a directory notice. Owners first: were one allowed, the probe
        # stops before async mode could make the kernel signal the API.
        (lambda: fcntl.fcntl(1, fcntl.F_SETOWN, api), eperm),
        (lambda: fcntl.fcntl(1, F_SETOWN_EX, struct.pack("=ii", 1, api)), eperm),  # F_OWNER_PID
        (lambda: fcntl.fcntl(0, F_SETOWN_EX, struct.pack("=ii", 2, api)), eperm),  # F_OWNER_PGRP
        (lambda: fcntl.ioctl(1, FIOSETOWN, struct.pack("=i", api)), eperm),
        (lambda: fcntl.ioctl(1, SIOCSPGRP, struct.pack("=i", api)), eperm),
        (lambda: fcntl.fcntl(1, F_SETSIG, signal.SIGTERM), eperm),
        (lambda: fcntl.fcntl(1, fcntl.F_SETFL, os.O_ASYNC), eperm),
        (lambda: fcntl.fcntl(0, fcntl.F_SETFL, os.O_ASYNC | os.O_NONBLOCK), eperm),
        (lambda: fcntl.ioctl(1, termios.FIOASYNC, struct.pack("=i", 1)), eperm),
        (lambda: fcntl.fcntl(0, F_SETLEASE, fcntl.F_RDLCK), eperm),
        (lambda: fcntl.fcntl(0, F_NOTIFY, 1), eperm),  # DN_ACCESS: ENOTDIR without the filter
        (lambda: fcntl.ioctl(1, termios.TIOCSPGRP, struct.pack("=i", api)), eperm),
        (lambda: fcntl.ioctl(1, termios.TIOCSCTTY, 0), eperm),
        (lambda: fcntl.ioctl(1, termios.TIOCSTI, b"x"), eperm),
        # Still allowed: the same calls on itself (glibc's raise and setrlimit use them).
        (lambda: os.kill(me, 0), None),
        (lambda: syscall("tgkill", me, me, 0), None),
        (lambda: resource.prlimit(0, resource.RLIMIT_NOFILE), None),
        (lambda: resource.prlimit(me, resource.RLIMIT_NOFILE), None),
        (lambda: os.sched_setaffinity(0, os.sched_getaffinity(0)), None),
        (lambda: os.sched_setscheduler(me, os.SCHED_OTHER, os.sched_param(0)), None),
        (lambda: fcntl.fcntl(1, fcntl.F_GETFL), None),
        (lambda: fcntl.fcntl(1, fcntl.F_SETFL, os.O_NONBLOCK), None),
        (lambda: fcntl.fcntl(1, fcntl.F_SETFL, 0), None),
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
        return refusal("ValueError" * (len(bytearray(1 << 30)) > 0))
    if MODE == "probe-native-memory":  # Pillow's own allocation past it
        from PIL import Image

        Image.new("RGBA", (12_000, 12_000))
        return refusal("ValueError")
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
    if (
        MODE == "probe-ignore-xcpu"
    ):  # past the soft CPU limit, ignoring its SIGXCPU, to the hard one
        signal.signal(signal.SIGXCPU, signal.SIG_IGN)
        resource.setrlimit(resource.RLIMIT_CPU, (1, 2))
        spin()
    if MODE == "probe-spin":  # busy past the parent's deadline
        spin()
    if MODE in ("probe-sleep", "probe-alarm"):
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
    if MODE == "probe-fork":  # no fork; no thread where the kernel counts them (Linux)
        try:
            pid = os.fork()
            if pid == 0:
                os._exit(0)
            os._exit(9)
        except OSError:
            pass
        try:
            thread = threading.Thread(target=lambda: None)
            thread.start()
            thread.join()
            if sys.platform == "linux":
                os._exit(10)
        except RuntimeError:
            pass
        forked: bytes = real_load().record(rest)
        return forked
    if MODE == "probe-x32":  # a syscall through the x32 ABI (x86_64 only): killed
        raw(0x40000000 | 39)
        return refusal("ValueError")
    if MODE == "probe-deny":  # every syscall the filter denies, and opens to write
        expect(deny_checks())
        denied: bytes = real_load().record(rest)
        return denied
    if MODE == "probe-mmap-write":  # a shared writable mapping of the upload: refused (EACCES)
        if sys.platform == "linux":
            if fcntl.fcntl(0, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY:
                os._exit(9)
            try:
                size = os.fstat(0).st_size
                mapped = mmap.mmap(0, size, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
                mapped[0:8] = b"MMAPWRIT"
                mapped.flush()
                os._exit(10)
            except PermissionError:
                pass
        mapless: bytes = real_load().record(rest)
        return mapless
    if MODE == "probe-group":  # a process group of its own, apart from the API's
        if os.getpgrp() != os.getpid():
            os._exit(9)
        grouped: bytes = real_load().record(rest)
        return grouped
    if MODE == "probe-parent":  # nothing reaches the API by its pid
        expect(parent_checks())
        checked: bytes = real_load().record(rest)
        return checked
    if MODE == "probe-environ":  # the API's own environment, through /proc: refused (EACCES)
        expect([(lambda: open(f"/proc/{os.getppid()}/environ", "rb").close(), 13)])
        refused: bytes = real_load().record(rest)
        return refused
    raise SystemExit(f"unknown probe {MODE}")


def fake_load() -> Any:
    if MODE == "probe-exit-before":
        os._exit(int(ARG))
    if MODE == "probe-crash-before":
        ctypes.string_at(0)
    if MODE == "probe-sleep-before":
        time.sleep(60)
    if MODE == "probe-spin-before":
        spin()
    return SimpleNamespace(record=record)


child.load = fake_load  # type: ignore[attr-defined]
if MODE == "probe-alarm":  # the backstop alarm, sooner than the parent's deadline
    child.ALARM = 1  # type: ignore[attr-defined]
sys.exit(child.main())
