"""The photo decoder's child process (ZIF-230). photos.py runs this file with `python -I -B`, an
empty environment and only stdin, stdout and stderr open, once per decode. stdin is the upload,
stdout a pipe. A decoder fault or a decompression bomb then ends this process, never the API.

The order is the point:
1. the resource limits, before anything reads the upload or imports Pillow;
2. the upload, at most MAX_BYTES + 1 bytes;
3. the decoder (photo_decode.py, by path: -I leaves the app off sys.path), then the READY byte;
4. the record photo_decode writes, then exit 0.

Imports only the standard library, and nothing from the app: photos.py imports it for the limits
and the exit codes. Exit codes before READY are the start failures photos.py answers 503 for; a
code after it is the decode's fault."""

import importlib.util
import resource
import sys
from pathlib import Path
from types import ModuleType

MIB = 1024 * 1024
MAX_BYTES = 10 * MIB  # an upload's cap (photos.MAX_BYTES)
# The child's address space. The heaviest legitimate decode measured (tests/test_photo_isolation.py)
# must stay under 80% of it.
MEMORY = 192 * MIB
CPU = (5, 6)  # seconds of CPU: SIGXCPU at the first, SIGKILL at the second
LIMITS: dict[int, tuple[int, int]] = {
    resource.RLIMIT_AS: (MEMORY, MEMORY),
    resource.RLIMIT_CPU: CPU,
    resource.RLIMIT_FSIZE: (0, 0),  # no regular file grows: a write fails with EFBIG
    resource.RLIMIT_NOFILE: (16, 16),
    resource.RLIMIT_NPROC: (0, 0),  # no fork and no thread (not enforced for root)
    resource.RLIMIT_CORE: (0, 0),
}
READY = b"R"
EXIT_LIMITS = 3
EXIT_IMPORT = 4
EXIT_MEMORY = 5
DECODER = Path(__file__).with_name("photo_decode.py")


def limit() -> None:
    """The limits, soft and hard alike, so this process cannot raise them again. On Linux this
    process is also the first the kernel's OOM killer picks (it still restarts the whole pod where
    the container's cgroup has memory.oom.group set, as the kubelet does on cgroup v2)."""
    if sys.platform == "linux":
        with open("/proc/self/oom_score_adj", "w") as adj:
            adj.write("1000")
    for which, (soft, hard) in LIMITS.items():
        # macOS refuses RLIMIT_AS and would not enforce it: development only, the image is Linux.
        if which == resource.RLIMIT_AS and sys.platform == "darwin":
            continue
        resource.setrlimit(which, (soft, hard))


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("photo_decode", DECODER)
    if spec is None or spec.loader is None:
        raise ImportError(str(DECODER))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    try:
        limit()
    except Exception:
        return EXIT_LIMITS
    data = sys.stdin.buffer.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:  # photos.py never sends more
        return 1
    try:
        decoder = load()
    except BaseException:
        return EXIT_IMPORT
    out = sys.stdout.buffer
    out.write(READY)
    out.flush()
    try:
        record: bytes = decoder.record(data)
    except MemoryError:
        return EXIT_MEMORY
    out.write(record)
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
