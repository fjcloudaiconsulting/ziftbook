"""The photo decoder's child process (ZIF-230). photos.py runs this file with `python -I -B`, an
empty environment and only stdin, stdout and stderr open, once per decode. stdin is the upload,
stdout a pipe. A decoder fault or a decompression bomb then ends this process, never the API.

The order is the point:
1. the resource limits and the syscall filter, before anything reads the upload or imports Pillow;
2. the upload, at most MAX_BYTES + 1 bytes;
3. the decoder (photo_decode.py, by path: -I leaves the app off sys.path), then the READY byte;
4. the record photo_decode writes, then exit 0.

Imports only the standard library, and nothing from the app: photos.py imports it for the limits
and the exit codes. Exit codes before READY are the start failures photos.py answers 503 for; a
code after it is the decode's fault."""

import ctypes
import errno
import importlib.util
import os
import resource
import struct
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


# A seccomp filter of syscalls the child never needs and a decoder bug could use (ZIF-230): the
# network (egress is open), io_uring (it would bypass the filter), and any change to the
# filesystem; open(at) with a write, create or truncate flag too. It stacks on the container
# runtime's own filter. Per architecture: aarch64 (production) and x86_64 (CI); another kills
# nothing and is refused (exit 3). A syscall from a foreign ABI (x32, a 32-bit one) is killed.
DENY = {
    "aarch64": {
        **{"socket": 198, "socketpair": 199, "io_uring_setup": 425},
        **{"mknodat": 33, "mkdirat": 34, "unlinkat": 35, "symlinkat": 36, "linkat": 37},
        **{"renameat": 38, "renameat2": 276, "truncate": 45, "ftruncate": 46},
        **{"fchmod": 52, "fchmodat": 53, "fchmodat2": 452, "openat2": 437},
    },
    "x86_64": {
        **{"socket": 41, "socketpair": 53, "io_uring_setup": 425},
        **{"mknod": 133, "mknodat": 259, "mkdir": 83, "mkdirat": 258, "rmdir": 84},
        **{"unlink": 87, "unlinkat": 263, "symlink": 88, "symlinkat": 266},
        **{"link": 86, "linkat": 265, "rename": 82, "renameat": 264, "renameat2": 316},
        **{"truncate": 76, "ftruncate": 77, "creat": 85, "openat2": 437},
        **{"chmod": 90, "fchmod": 91, "fchmodat": 268, "fchmodat2": 452},
    },
}
OPENS = {"aarch64": {56: 2}, "x86_64": {2: 1, 257: 2}}  # open(at): the index of its flags argument
AUDIT_ARCH = {"aarch64": 0xC00000B7, "x86_64": 0xC000003E}
WRITE_FLAGS = 0o1 | 0o2 | 0o100 | 0o1000  # O_WRONLY, O_RDWR, O_CREAT, O_TRUNC (both architectures)
X32 = 0x40000000
PR_SET_NO_NEW_PRIVS = 38
PR_SET_SECCOMP = 22
SECCOMP_MODE_FILTER = 2


def _bpf(code: int, jt: int, jf: int, k: int) -> bytes:
    return struct.pack("=HBBI", code, jt, jf, k)


def program(machine: str) -> bytes:
    """The filter as classic BPF over struct seccomp_data (nr at 0, arch at 4, args from 16)."""
    ld, jeq, jge, jset, ret = 0x20, 0x15, 0x35, 0x45, 0x06
    allow, kill, eperm = 0x7FFF0000, 0x80000000, 0x00050000 | errno.EPERM
    code = [_bpf(ld, 0, 0, 4), _bpf(jeq, 1, 0, AUDIT_ARCH[machine]), _bpf(ret, 0, 0, kill)]
    code.append(_bpf(ld, 0, 0, 0))
    if machine == "x86_64":
        code += [_bpf(jge, 0, 1, X32), _bpf(ret, 0, 0, kill)]
    for nr in sorted(DENY[machine].values()):
        code += [_bpf(jeq, 0, 1, nr), _bpf(ret, 0, 0, eperm)]
    for nr, flags in OPENS[machine].items():
        # Not this syscall: skip its four instructions, the number still loaded for the next.
        code += [_bpf(jeq, 0, 4, nr), _bpf(ld, 0, 0, 16 + 8 * flags)]
        code += [_bpf(jset, 0, 1, WRITE_FLAGS), _bpf(ret, 0, 0, eperm), _bpf(ret, 0, 0, allow)]
    code.append(_bpf(ret, 0, 0, allow))
    return b"".join(code)


class _Program(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]


def confine() -> None:
    """Install the filter (Linux only: development elsewhere). no_new_privs first, as the kernel
    requires of an unprivileged process (Kubernetes already sets it)."""
    if sys.platform != "linux":
        return
    code = program(os.uname().machine)  # KeyError on another architecture: refused
    buffer = ctypes.create_string_buffer(code)
    prog = _Program(len(code) // 8, ctypes.cast(buffer, ctypes.c_void_p))
    prctl = ctypes.CDLL(None, use_errno=True).prctl
    if prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "no_new_privs")
    if prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(prog), 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "seccomp")


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
        confine()
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
