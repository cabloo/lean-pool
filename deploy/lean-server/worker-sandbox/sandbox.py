#!/usr/bin/env python3
"""sandbox.py - put this process in a Landlock domain of its own, then start a Lean worker in it
(docs/worker-sandbox.md). The standard library only: it runs on the Kimina image's own Python.

    sandbox.py PROGRAM [ARGUMENT ...]    what lean-worker runs: sandbox, then exec PROGRAM
    sandbox.py --probe                   make the domain here, say what it covers, exit 0 or 1

A worker answers many checks in turn, and a check runs code inside it. Landlock (the kernel's own
sandbox for unprivileged processes, Linux 5.13 and later) then refuses that code, and everything
it starts:
  - every process outside its own domain through /proc and ptrace: another worker's pipes,
    memory and environment, and Kimina's own (every version);
  - creating, changing or removing files anywhere but /tmp, and writing any device but /dev/null;
    reading is not limited (every version), and changing a file's length by its path (truncate)
    is refused from version 3;
  - TCP, both ways: a check cannot call the server's own API on its loopback (version 4);
  - signals and abstract Unix sockets to processes outside its domain (version 6).
The program is started without the server's key: LEAN_SERVER_API_KEY and every variable whose
name holds KEY, TOKEN, SECRET or PASSWORD, in any case, are taken out of its environment, and
LEANPOOL_WORKER_SANDBOX=landlock-N is put in, which `kimina-ctl status` reads back.

A worker that cannot be sandboxed is not started: this exits 126 and says why on its error output
(Kimina keeps it). `kimina-ctl sandbox off` runs the workers without this.
"""

from __future__ import annotations

import ctypes
import errno
import os
import sys

MARK = "LEANPOOL_WORKER_SANDBOX"
PROBE_OK = "WORKER SANDBOX PROBE: ok - "
PROBE_FAILED = "WORKER SANDBOX PROBE: FAILED - "
SECRET_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD")
WRITABLE_DIRECTORY = "/tmp"
WRITABLE_DEVICE = "/dev/null"

# The system calls are numbered alike on every architecture Linux added them for (5.13).
_CREATE_RULESET, _ADD_RULE, _RESTRICT_SELF = 444, 445, 446
_PR_SET_NO_NEW_PRIVS = 38
_CREATE_RULESET_VERSION = 1
_RULE_PATH_BENEATH = 1

# Rights over files (include/uapi/linux/landlock.h). Only writes are handled: reading stays free.
_WRITE_FILE = 1 << 1
_REMOVE_DIR = 1 << 4
_REMOVE_FILE = 1 << 5
_MAKE_CHAR = 1 << 6
_MAKE_DIR = 1 << 7
_MAKE_REG = 1 << 8
_MAKE_SOCK = 1 << 9
_MAKE_FIFO = 1 << 10
_MAKE_BLOCK = 1 << 11
_MAKE_SYM = 1 << 12
_REFER = 1 << 13  # version 2
_TRUNCATE = 1 << 14  # version 3
_WRITES = (
    _WRITE_FILE
    | _REMOVE_DIR
    | _REMOVE_FILE
    | _MAKE_CHAR
    | _MAKE_DIR
    | _MAKE_REG
    | _MAKE_SOCK
    | _MAKE_FIFO
    | _MAKE_BLOCK
    | _MAKE_SYM
)
_BIND_TCP, _CONNECT_TCP = 1 << 0, 1 << 1  # version 4
_SCOPE_ABSTRACT_UNIX_SOCKET, _SCOPE_SIGNAL = 1 << 0, 1 << 1  # version 6


class SandboxError(Exception):
    """The domain could not be made; the message says why."""


class _RulesetAttr(ctypes.Structure):
    _fields_ = (
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    )


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = (("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32))


_libc = ctypes.CDLL(None, use_errno=True)
_libc.syscall.restype = ctypes.c_long


def _syscall(number: int, *arguments: object) -> int:
    result = _libc.syscall(ctypes.c_long(number), *arguments)
    if result < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return int(result)


def landlock_version() -> int:
    """The kernel's Landlock version, or SandboxError saying why there is none."""
    try:
        return _syscall(_CREATE_RULESET, None, ctypes.c_size_t(0), _CREATE_RULESET_VERSION)
    except OSError as error:
        if error.errno == errno.ENOSYS:
            raise SandboxError("this kernel has no Landlock (Linux 5.13 or later)") from None
        if error.errno == errno.EOPNOTSUPP:
            raise SandboxError("Landlock is in this kernel but not enabled at boot") from None
        if error.errno == errno.EPERM:
            raise SandboxError("Landlock's calls are refused here (a seccomp filter)") from None
        raise SandboxError(f"Landlock cannot be asked its version: {error.strerror}") from None


def covers(version: int) -> str:
    """What a domain of this version refuses, as `kimina-ctl status` says it."""
    parts = ["/proc", "files"]
    if version >= 4:
        parts.append("TCP")
    if version >= 6:
        parts.append("signals")
    return ", ".join(parts)


def enter_sandbox() -> int:
    """Put this process (and what it starts from now on) in a new domain; return the version."""
    version = landlock_version()
    handled_fs = _WRITES | (_REFER if version >= 2 else 0) | (_TRUNCATE if version >= 3 else 0)
    attr = _RulesetAttr(
        handled_access_fs=handled_fs,
        handled_access_net=(_BIND_TCP | _CONNECT_TCP) if version >= 4 else 0,
        scoped=(_SCOPE_ABSTRACT_UNIX_SOCKET | _SCOPE_SIGNAL) if version >= 6 else 0,
    )
    # The kernel reads the structure only as far as its version knows it.
    size = 8 if version < 4 else 16 if version < 6 else 24
    try:
        ruleset = _syscall(_CREATE_RULESET, ctypes.byref(attr), ctypes.c_size_t(size), 0)
    except OSError as error:
        raise SandboxError(f"Landlock {version} refused a ruleset: {error.strerror}") from None
    try:
        _allow(ruleset, WRITABLE_DIRECTORY, handled_fs)
        _allow(ruleset, WRITABLE_DEVICE, _WRITE_FILE | (_TRUNCATE if version >= 3 else 0))
        if _libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            code = ctypes.get_errno()
            raise SandboxError(f"no_new_privs cannot be set: {os.strerror(code)}")
        try:
            _syscall(_RESTRICT_SELF, ruleset, 0)
        except OSError as error:
            raise SandboxError(f"Landlock {version} refused the domain: {error.strerror}") from None
    finally:
        os.close(ruleset)
    return version


def _allow(ruleset: int, path: str, rights: int) -> None:
    try:
        descriptor = os.open(path, os.O_PATH | os.O_CLOEXEC)
    except OSError as error:
        raise SandboxError(f"cannot open {path}: {error.strerror}") from None
    try:
        rule = _PathBeneathAttr(allowed_access=rights, parent_fd=descriptor)
        _syscall(_ADD_RULE, ruleset, _RULE_PATH_BENEATH, ctypes.byref(rule), 0)
    except OSError as error:
        raise SandboxError(f"Landlock refused the rule for {path}: {error.strerror}") from None
    finally:
        os.close(descriptor)


def worker_environment(environment: dict[str, str], version: int) -> dict[str, str]:
    """The environment a worker starts with: no secret, and the sandbox's mark."""
    kept = {
        name: value
        for name, value in environment.items()
        if not any(word in name.upper() for word in SECRET_WORDS)
    }
    kept[MARK] = f"landlock-{version}"
    return kept


def main(arguments: list[str]) -> int:
    """Run as a launcher (exec the program; returns only on failure) or as the probe."""
    if arguments == ["--probe"]:
        try:
            version = enter_sandbox()
        except SandboxError as error:
            print(f"{PROBE_FAILED}{error}", flush=True)
            return 1
        print(f"{PROBE_OK}Landlock {version}: {covers(version)}", flush=True)
        return 0
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    if not arguments or arguments[0].startswith("-"):
        print("usage: sandbox.py PROGRAM [ARGUMENT ...] | sandbox.py --probe", file=sys.stderr)
        return 2
    try:
        version = enter_sandbox()
    except SandboxError as error:
        print(
            f"lean-worker: this worker cannot be sandboxed, so it is not started: {error} "
            "(kimina-ctl sandbox off runs workers without the sandbox)",
            file=sys.stderr,
            flush=True,
        )
        return 126
    try:
        os.execve(arguments[0], arguments, worker_environment(dict(os.environ), version))
    except OSError as error:
        print(f"lean-worker: cannot start {arguments[0]}: {error.strerror}", file=sys.stderr)
        return 127
    return 127  # not reached: execve returns only by raising


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
