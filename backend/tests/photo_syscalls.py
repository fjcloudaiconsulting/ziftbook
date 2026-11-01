"""ZIF-230: syscall numbers for the photo decoder's tests, kept apart from app/photo_child.py's
own tables so the two check each other: (aarch64, x86_64), None where an architecture has no such
call. Loaded by path in the child (tests/photo_child_probe.py), imported by the tests."""

# Calls the probes make by number.
NR: dict[str, tuple[int, int]] = {
    "tkill": (130, 200),
    "tgkill": (131, 234),
    "rt_sigqueueinfo": (138, 129),
    "rt_tgsigqueueinfo": (240, 297),
    "sched_setattr": (274, 314),
    "ioprio_get": (31, 252),
    "ioprio_set": (30, 251),
    "process_vm_writev": (271, 311),
    "kcmp": (272, 312),
    "read": (63, 0),
    "openat": (56, 257),
    "kill": (129, 62),
    "prlimit64": (261, 302),
}
# Every syscall the filter denies outright. Each is called with arguments that fail some other way
# (EFAULT, EINVAL, ENOENT...) or change nothing, so only the filter answers EPERM.
DENIED: dict[str, tuple[int | None, int | None]] = {
    "socket": (198, 41),
    "socketpair": (199, 53),
    "io_uring_setup": (425, 425),
    "mknod": (None, 133),
    "mknodat": (33, 259),
    "mkdir": (None, 83),
    "mkdirat": (34, 258),
    "rmdir": (None, 84),
    "unlink": (None, 87),
    "unlinkat": (35, 263),
    "symlink": (None, 88),
    "symlinkat": (36, 266),
    "link": (None, 86),
    "linkat": (37, 265),
    "rename": (None, 82),
    "renameat": (38, 264),
    "renameat2": (276, 316),
    "truncate": (45, 76),
    "ftruncate": (46, 77),
    "creat": (None, 85),
    "openat2": (437, 437),
    "chmod": (None, 90),
    "fchmod": (52, 91),
    "fchmodat": (53, 268),
    "fchmodat2": (452, 452),
    "setpriority": (140, 141),
    "ioprio_set": (30, 251),
    "pidfd_open": (434, 434),
    "pidfd_send_signal": (424, 424),
    "process_vm_writev": (271, 311),
    "kcmp": (272, 312),
}
# Arguments other than all zeros, where zeros would be refused by the kernel anyway.
DENIED_ARGS: dict[str, tuple[int, ...]] = {}
