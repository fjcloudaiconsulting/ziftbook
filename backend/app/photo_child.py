"""The photo decoder's child process (ZIF-230). photos.py runs this file with `python -I -B`, an
empty environment and only stdin, stdout and stderr open, once per decode. stdin is the upload,
stdout a pipe. A decoder fault or a decompression bomb then ends this process, never the API.

The order is the point:
1. the resource limits and the syscall filter, before anything reads the upload or imports Pillow;
2. the upload, at most MAX_BYTES + 1 bytes, then no longer dumpable;
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
import signal
import struct
import sys
from pathlib import Path
from types import ModuleType

MIB = 1024 * 1024
MAX_BYTES = 10 * MIB  # an upload's cap (photos.MAX_BYTES)
# The child's address space. The heaviest legitimate decode measured (tests/test_photo_isolation.py)
# must stay under 80% of it, and the pod's memory must hold it beside the API (photos.budget).
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
# Seconds after which the child ends itself (SIGALRM's default action): a backstop past the
# parent's deadline and its grace (photos.DECODE_TIMEOUT, REAP_GRACE), for a child the parent no
# longer watches. Not PR_SET_PDEATHSIG, which fires when the forking thread exits.
ALARM = 20
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
# network, io_uring (it would bypass the filter), any change to the filesystem (owners, times,
# extended attributes, holes and memfds included), open(at) with a write, create or truncate flag,
# and exec (which would make the child dumpable again). It stacks on the container
# runtime's own filter. Per architecture: aarch64 (production) and x86_64 (CI); another kills
# nothing and is refused (exit 3). A syscall from a foreign ABI (x32, a 32-bit one) is killed.
DENY = {
    "aarch64": {
        **{"socket": 198, "socketpair": 199, "io_uring_setup": 425},
        **{"mknodat": 33, "mkdirat": 34, "unlinkat": 35, "symlinkat": 36, "linkat": 37},
        **{"renameat": 38, "renameat2": 276, "truncate": 45, "ftruncate": 46},
        **{"fchmod": 52, "fchmodat": 53, "fchmodat2": 452, "openat2": 437},
        **{"setpriority": 140, "ioprio_set": 30, "pidfd_open": 434, "pidfd_send_signal": 424},
        **{"process_vm_writev": 271, "kcmp": 272},
        **{"execve": 221, "execveat": 281, "fallocate": 47, "memfd_create": 279},
        **{"setxattr": 5, "lsetxattr": 6, "fsetxattr": 7},
        **{"removexattr": 14, "lremovexattr": 15, "fremovexattr": 16},
        **{"utimensat": 88, "fchown": 55, "fchownat": 54},
        "ioctl": 29,
    },
    "x86_64": {
        **{"socket": 41, "socketpair": 53, "io_uring_setup": 425},
        **{"mknod": 133, "mknodat": 259, "mkdir": 83, "mkdirat": 258, "rmdir": 84},
        **{"unlink": 87, "unlinkat": 263, "symlink": 88, "symlinkat": 266},
        **{"link": 86, "linkat": 265, "rename": 82, "renameat": 264, "renameat2": 316},
        **{"truncate": 76, "ftruncate": 77, "creat": 85, "openat2": 437},
        **{"chmod": 90, "fchmod": 91, "fchmodat": 268, "fchmodat2": 452},
        **{"setpriority": 141, "ioprio_set": 251, "pidfd_open": 434, "pidfd_send_signal": 424},
        **{"process_vm_writev": 311, "kcmp": 312},
        **{"execve": 59, "execveat": 322, "fallocate": 285, "memfd_create": 319},
        **{"setxattr": 188, "lsetxattr": 189, "fsetxattr": 190},
        **{"removexattr": 197, "lremovexattr": 198, "fremovexattr": 199},
        **{"utimensat": 280, "utime": 132, "utimes": 235, "futimesat": 261},
        **{"fchown": 93, "fchownat": 260, "chown": 92, "lchown": 94},
        "ioctl": 16,
    },
}
# Syscalls that name a process, allowed only on the child itself: its pid as the first argument
# (SELF), or its pid or 0, which means itself (SELF_OR_ZERO). A same-uid kill, prlimit64 or
# scheduler call needs no capability and ignores dumpability, so without these a child taken over
# by a decoder bug could end or degrade the API. glibc's raise() and setrlimit() stay allowed.
SELF = {
    "aarch64": {"kill": 129, "tkill": 130, "tgkill": 131},
    "x86_64": {"kill": 62, "tkill": 200, "tgkill": 234},
}
SELF |= {
    "aarch64": SELF["aarch64"] | {"rt_sigqueueinfo": 138, "rt_tgsigqueueinfo": 240},
    "x86_64": SELF["x86_64"] | {"rt_sigqueueinfo": 129, "rt_tgsigqueueinfo": 297},
}
SELF_OR_ZERO = {
    "aarch64": {"prlimit64": 261, "sched_setparam": 118, "sched_setscheduler": 119},
    "x86_64": {"prlimit64": 302, "sched_setparam": 142, "sched_setscheduler": 144},
}
SELF_OR_ZERO |= {
    "aarch64": SELF_OR_ZERO["aarch64"] | {"sched_setaffinity": 122, "sched_setattr": 274},
    "x86_64": SELF_OR_ZERO["x86_64"] | {"sched_setaffinity": 203, "sched_setattr": 314},
}
# fcntl, allowed but for the commands that make the kernel signal a process: an owner for a
# descriptor's I/O signals (F_SETOWN, F_SETOWN_EX), the signal (F_SETSIG), a lease or a directory
# notice (F_SETLEASE, F_NOTIFY), and F_SETFL with O_ASYNC. Pointed at the API, a SIGIO (which ends
# a process by default) or a chosen signal would reach it. ioctl, which can do the same and which
# the decode never needs, is denied outright.
FCNTL = {"aarch64": 25, "x86_64": 72}
FCNTL_DENIED = (8, 15, 10, 1024, 1026)  # F_SETOWN, F_SETOWN_EX, F_SETSIG, F_SETLEASE, F_NOTIFY
F_SETFL = 4
O_ASYNC = 0o20000  # both architectures
OPENS = {"aarch64": {56: 2}, "x86_64": {2: 1, 257: 2}}  # open(at): the index of its flags argument
AUDIT_ARCH = {"aarch64": 0xC00000B7, "x86_64": 0xC000003E}
WRITE_FLAGS = 0o1 | 0o2 | 0o100 | 0o1000  # O_WRONLY, O_RDWR, O_CREAT, O_TRUNC (both architectures)
X32 = 0x40000000
PR_SET_DUMPABLE = 4
PR_SET_NO_NEW_PRIVS = 38
PR_SET_SECCOMP = 22
SECCOMP_MODE_FILTER = 2


def _bpf(code: int, jt: int, jf: int, k: int) -> bytes:
    return struct.pack("=HBBI", code, jt, jf, k)


def program(machine: str, pid: int) -> bytes:
    """The filter as classic BPF over struct seccomp_data (nr at 0, arch at 4, args from 16), for
    the process pid (the child: single-threaded, so its one thread's id is its pid too)."""
    ld, jeq, jge, jset, ret = 0x20, 0x15, 0x35, 0x45, 0x06
    allow, kill, eperm = 0x7FFF0000, 0x80000000, 0x00050000 | errno.EPERM
    code = [_bpf(ld, 0, 0, 4), _bpf(jeq, 1, 0, AUDIT_ARCH[machine]), _bpf(ret, 0, 0, kill)]
    code.append(_bpf(ld, 0, 0, 0))
    if machine == "x86_64":
        code += [_bpf(jge, 0, 1, X32), _bpf(ret, 0, 0, kill)]
    for nr in sorted(DENY[machine].values()):
        code += [_bpf(jeq, 0, 1, nr), _bpf(ret, 0, 0, eperm)]
    # A pid is an int: its argument's low 32 bits (a negative one, as -1 for all, differs).
    for nr in sorted(SELF[machine].values()):
        code += [_bpf(jeq, 0, 4, nr), _bpf(ld, 0, 0, 16), _bpf(jeq, 1, 0, pid)]
        code += [_bpf(ret, 0, 0, eperm), _bpf(ret, 0, 0, allow)]
    for nr in sorted(SELF_OR_ZERO[machine].values()):
        code += [_bpf(jeq, 0, 5, nr), _bpf(ld, 0, 0, 16), _bpf(jeq, 2, 0, pid), _bpf(jeq, 1, 0, 0)]
        code += [_bpf(ret, 0, 0, eperm), _bpf(ret, 0, 0, allow)]
    # fcntl(fd, cmd, arg): cmd is argument 1, arg argument 2.
    denied = len(FCNTL_DENIED)
    code += [_bpf(jeq, 0, denied + 6, FCNTL[machine]), _bpf(ld, 0, 0, 24)]
    code += [_bpf(jeq, denied - i + 2, 0, cmd) for i, cmd in enumerate(FCNTL_DENIED)]
    code += [_bpf(jeq, 0, 3, F_SETFL), _bpf(ld, 0, 0, 32), _bpf(jset, 0, 1, O_ASYNC)]
    code += [_bpf(ret, 0, 0, eperm), _bpf(ret, 0, 0, allow)]
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
    code = program(os.uname().machine, os.getpid())  # KeyError elsewhere: refused
    buffer = ctypes.create_string_buffer(code)
    prog = _Program(len(code) // 8, ctypes.cast(buffer, ctypes.c_void_p))
    prctl = ctypes.CDLL(None, use_errno=True).prctl
    if prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "no_new_privs")
    if prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(prog), 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "seccomp")


def undumpable() -> None:
    """No core file of the upload, whatever the host's core_pattern (a piped pattern ignores
    RLIMIT_CORE), and no other process of this uid reading this one through /proc. Set once the
    upload is read, so a test can still inspect the child while it waits for it (Linux only)."""
    if sys.platform != "linux":
        return
    if ctypes.CDLL(None, use_errno=True).prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "dumpable")


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("photo_decode", DECODER)
    if spec is None or spec.loader is None:
        raise ImportError(str(DECODER))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    signal.alarm(ALARM)
    try:
        limit()
        confine()
    except Exception:
        return EXIT_LIMITS
    data = sys.stdin.buffer.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:  # photos.py never sends more
        return 1
    try:
        undumpable()
    except Exception:
        return EXIT_LIMITS
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
