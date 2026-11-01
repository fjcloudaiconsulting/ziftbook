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
