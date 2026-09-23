"""The sidecar's single-instance invariant, held by the OS rather than by a file.

Why a lock and not the PID file
-------------------------------

A PID file is a record of intent, and an invariant cannot rest on a record.
Several code paths can delete it, the process-identity heuristic that reads it
can legitimately answer "cannot tell", and any check-then-write built on it
leaves a window, as wide as the daemon's boot, in which a second launcher can
also conclude that no sidecar is running. Every program that can be wrong about
the record can break the invariant.

This module holds the invariant with an **exclusive advisory lock, held open
for the process's lifetime**. Three properties follow, and they are the whole
point:

1. **Acquisition is atomic.** One syscall decides the winner. There is no
   check-then-act window for a second launcher to slip into, however slow the
   loser's boot is.
2. **Release is the kernel's job.** The lock is a property of an open handle,
   so it drops when the handle closes, including on ``TerminateProcess``, a
   segfault, or a power-off. No cleanup code has to run, and none can be
   skipped.
3. **Deleting the file does not let a second instance in.** On Windows the open
   handle blocks the unlink outright. On POSIX it does not: the lock belongs to
   the open file description, so the owner keeps holding it, but a newcomer
   opening the path with ``O_CREAT`` would create a *fresh inode* and lock that
   instead. A file lock alone is therefore defeatable on POSIX by deleting its
   file, which is why Linux additionally takes an abstract-namespace name (see
   below).

   ``AF_UNIX`` abstract sockets exist only on Linux. On other POSIX hosts
   (macOS, the BSDs) the file lock stands alone, and the residual exposure is
   "something deletes ``runtime/sidecar.lock`` while a daemon is running".
   Nothing in work-buddy does: unlike the PID file, this file has no remover
   anywhere in the codebase except its own owner's :func:`release`.

What this module deliberately does not do
-----------------------------------------

It does not identify or terminate anything. Asking "which process owns this?"
re-introduces the heuristic the lock exists to retire. The lock answers
"is it owned?", which is the only question mutual exclusion needs. Ownership
metadata written alongside (:func:`read_owner`) is best-effort and for humans:
it names a likely owner in an error message and is never trusted for a
decision.

Relationship to ``sidecar/pid.py``
----------------------------------

The PID file is the human- and tool-readable record of who is running, and it
is what ``stop`` aims at. It does not *prevent* a second instance: this module
does. That separation is what makes the PID file safe to be wrong about.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from work_buddy.logging_config import get_logger
from work_buddy.paths import resolve

logger = get_logger(__name__)

LOCK_FILE = resolve("runtime/sidecar-lock")

# Windows locks exactly one byte at offset 0 (``msvcrt`` offers only byte-range
# locking); POSIX uses ``flock``, which covers the whole file and ignores these.
# Either way the locked bytes are never read: owner metadata lives in a separate
# sibling file, because a Windows byte-range lock is mandatory and a reader
# overlapping it would fail rather than block.
_LOCK_OFFSET = 0
_LOCK_LENGTH = 1


def _owner_file() -> Path:
    """Sibling of :data:`LOCK_FILE` holding best-effort owner metadata.

    Derived from ``LOCK_FILE`` rather than resolved independently so tests that
    redirect the lock path get the companion for free.
    """
    return LOCK_FILE.with_name(LOCK_FILE.name + ".owner.json")


class InstanceLockHeld(Exception):
    """Another live process holds the sidecar instance lock.

    Carries the best-effort owner description for the operator-facing message.
    Callers must treat this as final: the lock is the invariant, so there is no
    correct way to proceed past it.
    """

    def __init__(self, owner: dict | None = None) -> None:
        self.owner = owner or {}
        pid = self.owner.get("pid")
        detail = f" (likely pid={pid})" if pid else ""
        super().__init__(
            f"Another work-buddy sidecar already holds the instance lock{detail}."
        )


class InstanceLockUnavailable(Exception):
    """The lock could not be acquired *or* proven free.

    Distinct from :class:`InstanceLockHeld`: this is an I/O or platform failure
    (unwritable data root, missing ``fcntl``/``msvcrt``), not a second daemon.
    Callers must still refuse to start, because an unenforceable invariant is
    exactly the state this module exists to eliminate, but the operator message
    differs, because the remedy does.
    """


@dataclass(frozen=True)
class InstanceLock:
    """A held lock. Opaque to callers except for :meth:`release`.

    ``sock`` is the Linux abstract-namespace socket when that layer applies,
    else ``None``. It is held only to keep the name reserved for the process's
    lifetime; nothing is ever sent or accepted on it.
    """

    fd: int
    path: Path
    sock: object | None = None

    def release(self) -> None:
        """Drop the lock and close the handle. Idempotent and never raises.

        Calling this is optional. The kernel releases the lock when the process
        dies by any means; an explicit release only shortens the window between
        a graceful shutdown and the next start.
        """
        _release_fd(self.fd, self.path, self.sock)


# Module-level so the handle outlives every local scope. If the only reference
# to the fd were a local, a garbage-collected InstanceLock could close it and
# silently drop the invariant mid-run.
_held: InstanceLock | None = None


def _lock_bytes(fd: int) -> bool:
    """Take the exclusive lock without blocking. ``False`` if already held.

    Raises ``OSError`` for anything that is not contention, so callers can tell
    "someone else owns it" from "locking does not work here".

    POSIX uses ``flock``, not ``lockf``, and the choice is deliberate.
    ``lockf``/``F_SETLK`` locks are owned by the *process*, with two
    consequences that would quietly break :func:`is_locked`: a second
    ``lockf`` from the same process succeeds (so a probe would report the lock
    free while we hold it), and closing *any* descriptor on the file releases
    all of that process's locks on it (so a probe that opened and closed a
    second fd would drop the real lock). ``flock`` is owned by the open file
    description, so a second ``open()`` contends normally and closing it
    affects nothing else.
    """
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, _LOCK_LENGTH)
        except OSError as exc:
            # msvcrt reports a failed non-blocking lock as EDEADLOCK (36);
            # EACCES (13) appears on some builds. Anything else is a real
            # fault and must not be mistaken for "another daemon owns this".
            if exc.errno in (13, 36):  # EACCES, EDEADLOCK
                return False
            raise
        return True

    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (11, 13, 35):  # EAGAIN, EACCES, EWOULDBLOCK
            return False
        raise
    return True


def _unlock_bytes(fd: int) -> None:
    """Best-effort release. Closing the fd also releases it."""
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, _LOCK_LENGTH)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def _abstract_name() -> bytes | None:
    """Linux abstract-namespace socket name for this data root, else ``None``.

    Abstract ``AF_UNIX`` names live in the kernel, not the filesystem, so there
    is no directory entry for anything to delete and the name is released when
    the last holder's socket closes. That is the property the file lock lacks on
    POSIX, where deleting the lock file lets a newcomer create and lock a
    different inode.

    The name is derived from the *resolved* lock path, so two checkouts or two
    users with different data roots get different names and do not exclude each
    other. work-buddy is multi-user, and a single global name would make a
    second account's sidecar refuse to start.
    """
    if not sys.platform.startswith("linux"):
        return None
    import hashlib

    try:
        key = str(LOCK_FILE.resolve())
    except OSError:
        key = str(LOCK_FILE)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    # Leading NUL selects the abstract namespace. Kept well inside the 108-byte
    # sun_path limit.
    return b"\0work-buddy-sidecar-" + digest.encode("ascii")


def _bind_abstract() -> "object | None | bool":
    """Reserve the Linux abstract name.

    Returns the bound socket on success, ``False`` when another process holds
    the name, and ``None`` when this platform has no abstract namespace (so
    there is nothing to reserve and nothing to check).
    """
    name = _abstract_name()
    if name is None:
        return None
    import socket

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except (OSError, AttributeError) as exc:
        # A sandbox or a kernel without AF_UNIX. This layer is a hardening of
        # the file lock, not a replacement for it, so degrade rather than
        # refuse to start.
        logger.debug("Abstract-namespace guard unavailable: %s", exc)
        return None
    try:
        sock.bind(name)
    except OSError as exc:
        sock.close()
        if exc.errno in (98, 48):  # EADDRINUSE (Linux, BSD)
            return False
        logger.debug("Abstract-namespace guard unavailable: %s", exc)
        return None
    return sock


def _release_fd(fd: int, path: Path, sock: object | None = None) -> None:
    global _held
    _unlock_bytes(fd)
    try:
        os.close(fd)
    except OSError:
        pass
    if sock is not None:
        try:
            sock.close()  # type: ignore[attr-defined]
        except OSError:
            pass
    if _held is not None and _held.fd == fd:
        _held = None
    try:
        _owner_file().unlink(missing_ok=True)
    except OSError:
        pass
    logger.debug("Released sidecar instance lock: %s", path)


def _verify_same_inode(fd: int, path: Path) -> bool:
    """Confirm the locked handle still refers to the file at ``path``.

    A lock taken on a file that another process has since unlinked and
    recreated guards something nobody else will ever open, and mutual exclusion
    is silently gone. Windows refuses to unlink an open handle, so this cannot
    happen there, but the check is unconditional because a cheap invariant
    assertion should not be platform-conditional.
    """
    try:
        held = os.fstat(fd)
        current = path.stat()
    except OSError:
        return False
    return (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino)


def _write_owner(pid: int) -> None:
    """Record best-effort owner metadata beside the lock. Never raises.

    Failure is tolerable by design: this file informs error messages and
    nothing decides on it. The lock is already held by the time we get here.
    """
    from work_buddy.utils.process import process_start_token

    payload = {
        "pid": pid,
        "start_token": process_start_token(pid),
        "argv": sys.argv[:4],
        "session_id": os.environ.get("WORK_BUDDY_SESSION_ID"),
    }
    owner_path = _owner_file()
    try:
        owner_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=owner_path.parent, prefix=".sidecar_owner_", suffix=".tmp"
        )
        try:
            os.write(fd, json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp, owner_path)
    except OSError as exc:
        logger.debug("Could not record instance-lock owner: %s", exc)


def read_owner() -> dict | None:
    """Best-effort owner metadata, or ``None``. Informational only.

    The returned pid may be stale, may name a reused pid, or may be absent
    while the lock is genuinely held. Never gate a decision on it; use
    :func:`is_locked`.
    """
    try:
        value = json.loads(_owner_file().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def acquire(*, owner_pid: int | None = None) -> InstanceLock:
    """Take the single-instance lock, or raise.

    Call this as the **first** action of sidecar boot, before any import-heavy
    work. Everything between process start and this call is a window in which a
    second launcher can win too, and the whole point of the lock is that the
    window is one syscall wide.

    Raises :class:`InstanceLockHeld` when another sidecar owns it, and
    :class:`InstanceLockUnavailable` when locking could not be performed at
    all. Both are terminal for the caller.
    """
    global _held
    if _held is not None:
        return _held

    path = LOCK_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as exc:
        raise InstanceLockUnavailable(
            f"Could not open the sidecar instance lock at {path}: {exc}"
        ) from exc

    try:
        got = _lock_bytes(fd)
    except (OSError, ImportError) as exc:
        os.close(fd)
        raise InstanceLockUnavailable(
            f"Byte-range locking is unavailable on this host ({exc}); refusing "
            "to start a sidecar whose single-instance invariant cannot be "
            "enforced."
        ) from exc

    if not got:
        os.close(fd)
        raise InstanceLockHeld(read_owner())

    if not _verify_same_inode(fd, path):
        # Someone replaced the lock file between open and lock. Our lock guards
        # an orphaned inode, which enforces nothing.
        _release_fd(fd, path)
        raise InstanceLockUnavailable(
            f"The sidecar instance lock file at {path} was replaced during "
            "acquisition; retry."
        )

    # Second layer, Linux only. Reaching here with the abstract name already
    # taken means a daemon is running whose lock file was deleted out from
    # under it, so our file lock is on a fresh inode and proves nothing. That
    # is precisely the case the file lock cannot see on POSIX.
    sock = _bind_abstract()
    if sock is False:
        _release_fd(fd, path)
        raise InstanceLockHeld(read_owner())

    lock = InstanceLock(fd=fd, path=path, sock=sock)
    _held = lock
    # Stamp the file's mtime so it reads as "when the current owner acquired
    # this". ``O_CREAT`` on an existing file does not touch mtime, and nothing
    # here writes to the locked byte, so without this the timestamp would be
    # from whenever the file was first created, possibly months ago. Callers
    # use the age as a boot-grace signal (``cli/lifecycle._lock_age_s``): a
    # stale timestamp would make every freshly booting daemon, which holds the
    # lock but has not yet written its PID file, look wedged.
    try:
        os.utime(path, None)
    except OSError as exc:
        logger.debug("Could not stamp instance-lock mtime: %s", exc)
    _write_owner(owner_pid if owner_pid is not None else os.getpid())
    logger.info("Sidecar instance lock acquired: %s (pid=%d)", path, os.getpid())
    return lock


def release() -> None:
    """Release the lock held by this process, if any. Never raises."""
    if _held is not None:
        _held.release()


def is_locked() -> bool:
    """``True`` if some live process holds the lock.

    This is the authoritative "is a sidecar running?" question, and unlike the
    PID file it cannot be wrong: it probes the kernel's own state. It is also
    non-destructive, which is why status surfaces use it.

    A process that already holds the lock short-circuits on ``_held`` rather
    than probing, so the daemon never contends with itself.
    """
    if _held is not None:
        return True

    # The abstract name is authoritative where it exists, and it answers even
    # when the lock file is gone, which is the state a file-only probe reads as
    # "nothing running".
    probe = _bind_abstract()
    if probe is False:
        return True
    if probe is not None:
        probe.close()  # type: ignore[attr-defined]

    try:
        if not LOCK_FILE.exists():
            return False
        fd = os.open(LOCK_FILE, os.O_RDWR)
    except OSError:
        # Unopenable is not provably free, and callers of a boolean cannot act
        # on "unknown". Reporting locked is the conservative answer: it stops a
        # spawn rather than causing one.
        return True
    try:
        got = _lock_bytes(fd)
    except (OSError, ImportError):
        return True
    if got:
        _unlock_bytes(fd)
    try:
        os.close(fd)
    except OSError:
        pass
    return not got


def _process_table() -> list[tuple[int, int, str]]:
    """``(pid, ppid, command_line)`` for candidate Python processes. Never raises."""
    import subprocess

    if sys.platform == "win32":
        # The separator is ``[char]9`` rather than a backtick-t escape: this is
        # a single-quoted PowerShell string, where backtick is literal, so
        # "`t" would emit the two characters and every row would fail to split.
        script = (
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe' "
            "or Name='pythonw.exe'\" | ForEach-Object { "
            "'{0}{3}{1}{3}{2}' -f $_.ProcessId, $_.ParentProcessId, "
            "$_.CommandLine, [char]9 }"
        )
        argv = ["powershell.exe", "-NoProfile", "-Command", script]
        sep = "\t"
    else:
        argv = ["ps", "-eo", "pid=,ppid=,args="]
        sep = None

    from work_buddy.compat import subprocess_creation_flags

    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=20,
            # CREATE_NO_WINDOW on Windows: a background diagnostic must never
            # put a console window on the user's screen.
            creationflags=subprocess_creation_flags(),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []

    rows: list[tuple[int, int, str]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(sep, 2) if sep else line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
        except ValueError:
            continue
    return rows


def find_sidecar_processes(*, exclude_pid: int | None = None) -> list[int]:
    """PIDs of *foreign* processes that look like a sidecar daemon.

    Defence in depth for detectability, not part of the invariant: the lock
    already prevents duplicates, so a non-empty result means something escaped
    it and an operator needs to know. Best-effort and heuristic; never
    terminate on the strength of this alone.

    "Foreign" excludes the caller's whole process tree, and that exclusion is
    required rather than tidy. On Windows a uv/virtualenv ``python.exe`` is a
    **trampoline** that re-execs the real interpreter, so one logical sidecar
    appears in the process table as two processes carrying the same
    ``-m work_buddy.sidecar`` command line: a ~3 MB parent and the real
    ~280 MB child. Reporting an ancestor or descendant would fire this alarm
    on every single boot, and an alarm that always fires is not an alarm.
    """
    me = os.getpid() if exclude_pid is None else exclude_pid
    rows = _process_table()
    if not rows:
        return []

    parents = {pid: ppid for pid, ppid, _ in rows}

    # Our ancestors: walk up from `me` (bounded, since the table only holds Python
    # processes, so the walk stops as soon as it leaves the interpreter chain).
    related = {me}
    cursor = me
    for _ in range(64):
        cursor = parents.get(cursor, 0)
        if cursor in (0, 1) or cursor in related:
            break
        related.add(cursor)

    # Our descendants: repeatedly absorb children of anything already related.
    for _ in range(64):
        grew = False
        for pid, ppid, _cmd in rows:
            if ppid in related and pid not in related:
                related.add(pid)
                grew = True
        if not grew:
            break

    found = [
        pid
        for pid, _ppid, cmd in rows
        if pid not in related
        and "-m work_buddy.sidecar" in cmd.replace("\\", "/")
    ]
    return sorted(set(found))
