"""Read exact OS process arguments without flattening paths or logging argv.

macOS SDK sys/sysctl.h defines CTL_KERN=1 and KERN_PROCARGS2=49. The
KERN_PROCARGS2 buffer contains argc, executable path, padding, then argv strings.
Linux uses procfs. Unsupported/inaccessible processes fail closed.
"""
from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProcessIdentity:
    executable: Path
    argv: tuple[str, ...]


def _decode_macos_procargs(payload: bytes) -> ProcessIdentity | None:
    if len(payload) < 5:
        return None
    argc = int.from_bytes(payload[:4], byteorder=sys.byteorder, signed=True)
    if not 1 <= argc <= 65536:
        return None
    executable_end = payload.find(b'\0', 4)
    if executable_end <= 4:
        return None
    executable = Path(os.fsdecode(payload[4:executable_end]))
    cursor = executable_end
    while cursor < len(payload) and payload[cursor] == 0:
        cursor += 1
    argv = []
    for _ in range(argc):
        end = payload.find(b'\0', cursor)
        if end < cursor:
            return None
        argv.append(os.fsdecode(payload[cursor:end]))
        cursor = end + 1
    return ProcessIdentity(executable, tuple(argv))


def _macos_identity(pid: int) -> ProcessIdentity | None:
    libc = ctypes.CDLL(None, use_errno=True)
    sysctl = libc.sysctl
    sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint,
                       ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                       ctypes.c_void_p, ctypes.c_size_t]
    sysctl.restype = ctypes.c_int
    mib = (ctypes.c_int * 3)(1, 49, pid)
    size = ctypes.c_size_t(os.sysconf('SC_ARG_MAX'))
    buffer = ctypes.create_string_buffer(size.value)
    if sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
        return None
    return _decode_macos_procargs(buffer.raw[:size.value])


def read_process_identity(pid: int) -> ProcessIdentity | None:
    if pid <= 1:
        return None
    try:
        if sys.platform == 'darwin':
            return _macos_identity(pid)
        if sys.platform.startswith('linux'):
            proc = Path('/proc') / str(pid)
            executable = (proc / 'exe').resolve(strict=True)
            payload = (proc / 'cmdline').read_bytes()
            argv = tuple(os.fsdecode(value) for value in payload.rstrip(b'\0').split(b'\0'))
            return ProcessIdentity(executable, argv) if payload else None
    except (OSError, ValueError, AttributeError):
        return None
    return None


def process_exists(pid: int) -> bool:
    if pid <= 1:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def postmaster_matches(pid: int, executable: Path, cluster: Path) -> bool:
    identity = read_process_identity(pid)
    if identity is None or identity.executable.resolve() != executable.resolve():
        return False
    # PostgreSQL accepts these equivalent -D forms. Exactly one explicit data
    # directory is required; conflicting/repeated options fail closed.
    directories: list[str] = []
    iterator = iter(identity.argv[1:])
    for argument in iterator:
        if argument in {'-D', '--pgdata'}:
            value = next(iterator, None)
            if value is None:
                return False
            directories.append(value)
        elif argument.startswith('--pgdata='):
            directories.append(argument.partition('=')[2])
        elif argument.startswith('-D'):
            directories.append(argument[2:])
    if len(directories) != 1 or not Path(directories[0]).is_absolute():
        return False
    return Path(directories[0]).resolve() == cluster.resolve()
