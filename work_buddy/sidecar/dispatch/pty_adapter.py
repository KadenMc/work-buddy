"""Cross-platform PTY adapter for interactive agent sessions.

Provides a thin abstraction over platform-specific pseudo-terminal
backends so the executor can spawn interactive ``claude`` sessions
programmatically — sessions that appear in the user's Claude Code
Desktop/CLI session picker.

Architecture
------------
- **Windows**: ``pywinpty`` (wraps Windows ConPTY).
- **POSIX** (Linux/macOS): ``ptyprocess.PtyProcessUnicode``.
- **Common surface**: ``PTYSession`` with ``spawn``, ``write``,
  ``sendline``, ``readline``, ``read_until``, ``expect_pattern``,
  ``is_alive``, and ``close``.

The adapter is used **only for session creation**. Once an interactive
session exists, it can be continued by the user in the Claude Code
picker, or programmatically via ``claude --resume <id> --print``
without needing the PTY layer again.

Usage::

    session = PTYSession.spawn(["claude", "initial prompt", "--name", "daemon:my-job"])
    output = session.read_until(prompt_pattern, timeout=120)
    session_id = parse_session_id(output)
    session.close()  # Session persists in Claude's storage, visible in picker
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, Sequence

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

BackendKind = Literal["windows", "posix"]


# ---------------------------------------------------------------------------
# Protocol: what any PTY backend must look like
# ---------------------------------------------------------------------------

class PTYProcessLike(Protocol):
    """Minimal common PTY process surface."""

    def write(self, s: str) -> Any: ...
    def readline(self) -> str: ...
    def read(self, size: int = ...) -> str: ...
    def isalive(self) -> bool: ...


# ---------------------------------------------------------------------------
# Backend detection
# ---------------------------------------------------------------------------

def detect_backend() -> BackendKind:
    """Detect which PTY backend to use based on the current OS."""
    system = platform.system()
    if system == "Windows":
        return "windows"
    if system in {"Linux", "Darwin"}:
        return "posix"
    raise NotImplementedError(f"Unsupported OS for PTY: {system!r}")


def _check_backend_available(backend: BackendKind) -> None:
    """Check that the required PTY library is importable.

    Raises ImportError with a helpful message if not.
    """
    if backend == "windows":
        try:
            import winpty  # noqa: F401
        except ImportError:
            raise ImportError(
                "pywinpty is required for interactive agent sessions on Windows. "
                "Install with: poetry add pywinpty"
            )
    else:
        try:
            import ptyprocess  # noqa: F401
        except ImportError:
            raise ImportError(
                "ptyprocess is required for interactive agent sessions on POSIX. "
                "Install with: poetry add ptyprocess"
            )


# ---------------------------------------------------------------------------
# PTYSession — the cross-platform adapter
# ---------------------------------------------------------------------------

@dataclass
class PTYSession:
    """Cross-platform PTY session wrapper.

    Wraps platform-specific PTY backends behind a common API for
    spawning interactive Claude sessions.

    The public API accepts argv-style commands. Backend-specific
    command rendering (argv vs command-line string) is handled
    internally.
    """

    backend: BackendKind
    proc: Any  # PTYProcessLike — typed as Any to avoid import-time dependency
    _buffer: str = field(default="", repr=False)
    _closed: bool = field(default=False, repr=False)

    @classmethod
    def spawn(
        cls,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        cols: int = 120,
        rows: int = 40,
    ) -> PTYSession:
        """Spawn a PTY-backed child process.

        Args:
            argv: Command and arguments, e.g.
                ``["claude", "initial prompt", "--name", "daemon:job"]``.
            cwd: Working directory for the child process.
            env: Environment variables. Defaults to inheriting ``os.environ``.
            cols: Terminal width (columns).
            rows: Terminal height (rows).

        Returns:
            A live PTYSession bound to the correct backend.
        """
        backend = detect_backend()
        _check_backend_available(backend)

        spawn_env = dict(os.environ)
        if env:
            spawn_env.update(env)

        logger.info(
            "PTY spawn: backend=%s, cmd=%s, cwd=%s",
            backend, argv[0] if argv else "?", cwd,
        )

        if backend == "windows":
            proc = _spawn_windows(argv, cwd=cwd, env=spawn_env, cols=cols, rows=rows)
        else:
            proc = _spawn_posix(argv, cwd=cwd, env=spawn_env, cols=cols, rows=rows)

        return cls(backend=backend, proc=proc)

    # --- Writing ---

    def write(self, text: str) -> None:
        """Write raw text to the child process."""
        if self._closed:
            raise RuntimeError("PTY session is closed.")
        self.proc.write(text)

    def sendline(self, line: str) -> None:
        """Send one line of input (appends \\r\\n)."""
        self.write(line + "\r\n")

    # --- Reading ---

    def readline(self, timeout: float = 30.0) -> str:
        """Read one line of output.

        Args:
            timeout: Max seconds to wait for a complete line.

        Returns:
            The next line from the child, stripped of trailing newline.

        Raises:
            TimeoutError: If no complete line arrives within timeout.
        """
        deadline = time.monotonic() + timeout

        while "\n" not in self._buffer:
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"PTY readline timed out after {timeout}s. "
                    f"Buffer so far: {self._buffer[-200:]!r}"
                )
            if not self.is_alive():
                break
            chunk = self._read_chunk()
            if chunk:
                self._buffer += chunk
            else:
                time.sleep(0.05)

        if "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            return line.rstrip("\r")

        # Process died — return whatever's left
        remaining = self._buffer
        self._buffer = ""
        return remaining.rstrip("\r\n")

    def read_until(
        self,
        pattern: str | re.Pattern,
        timeout: float = 120.0,
    ) -> str:
        """Read output until a regex pattern matches.

        Accumulates output and tests the full buffer against the
        pattern after each chunk. Returns all output accumulated
        up to and including the match.

        Args:
            pattern: Regex pattern (string or compiled) to wait for.
            timeout: Max seconds to wait for the pattern.

        Returns:
            All output up to and including the pattern match.

        Raises:
            TimeoutError: If the pattern doesn't appear within timeout.
        """
        if isinstance(pattern, str):
            pattern = re.compile(pattern, re.DOTALL)

        deadline = time.monotonic() + timeout
        accumulated = self._buffer
        self._buffer = ""

        while True:
            match = pattern.search(accumulated)
            if match:
                # Put everything after the match back in the buffer
                self._buffer = accumulated[match.end():]
                return accumulated[:match.end()]

            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"PTY read_until timed out after {timeout}s waiting for "
                    f"pattern {pattern.pattern!r}. "
                    f"Last 500 chars: {accumulated[-500:]!r}"
                )

            if not self.is_alive():
                raise RuntimeError(
                    f"PTY process died while waiting for pattern "
                    f"{pattern.pattern!r}. Output so far: {accumulated[-500:]!r}"
                )

            chunk = self._read_chunk()
            if chunk:
                accumulated += chunk
            else:
                time.sleep(0.05)

    def read_available(self, timeout: float = 0.5) -> str:
        """Read whatever output is currently available.

        Waits up to ``timeout`` seconds for at least some output,
        then returns everything buffered.
        """
        deadline = time.monotonic() + timeout
        result = self._buffer
        self._buffer = ""

        while time.monotonic() < deadline:
            chunk = self._read_chunk()
            if chunk:
                result += chunk
            elif result:
                break  # Got something, no more coming immediately
            else:
                time.sleep(0.05)

        return result

    # --- Lifecycle ---

    def is_alive(self) -> bool:
        """Check whether the child process is still running."""
        if self._closed:
            return False
        try:
            return bool(self.proc.isalive())
        except Exception:
            return False

    def close(self, force: bool = False) -> None:
        """Close the PTY session.

        For interactive Claude sessions, closing the PTY does NOT
        delete the session — it persists in Claude's storage and
        remains visible in the session picker.

        Args:
            force: If True, forcefully terminate the child process.
                If False (default), send EOF/exit gracefully first.
        """
        if self._closed:
            return
        self._closed = True

        try:
            if self.backend == "windows":
                _close_windows(self.proc, force=force)
            else:
                _close_posix(self.proc, force=force)
        except Exception as exc:
            logger.warning("PTY close error (non-fatal): %s", exc)

        logger.info("PTY session closed (backend=%s, force=%s)", self.backend, force)

    # --- Internal ---

    def _read_chunk(self) -> str:
        """Read a chunk of output from the backend.

        Non-blocking: returns empty string if nothing available.
        """
        try:
            if self.backend == "windows":
                return _read_windows(self.proc)
            else:
                return _read_posix(self.proc)
        except EOFError:
            return ""
        except Exception:
            return ""


# ---------------------------------------------------------------------------
# Windows backend (pywinpty)
# ---------------------------------------------------------------------------

def _spawn_windows(
    argv: Sequence[str],
    *,
    cwd: str | None,
    env: dict[str, str],
    cols: int,
    rows: int,
) -> Any:
    """Spawn via pywinpty (ConPTY).

    Pass argv as a **list**, not a pre-built command string. pywinpty's
    ``spawn()`` calls ``subprocess.list2cmdline()`` internally on
    ``argv[1:]``. If we pre-quote with ``list2cmdline`` and pass a
    string, pywinpty ``shlex.split``s it back and re-quotes — causing
    double-quoting where space-containing arguments get literal ``"``
    characters baked into their content.
    """
    from winpty import PtyProcess

    logger.debug("Windows PTY argv: %s", list(argv))

    proc = PtyProcess.spawn(
        list(argv),
        cwd=cwd,
        dimensions=(rows, cols),
    )
    return proc


class _WindowsReaderThread:
    """Persistent background reader for pywinpty.

    pywinpty's ``read()`` blocks until data is available, making
    non-blocking reads impossible with per-call threads (abandoned
    threads hold the read lock). This class runs a single persistent
    reader thread that continuously reads from the PTY and queues
    chunks for the main thread to consume.
    """

    def __init__(self, proc: Any) -> None:
        import threading
        import queue

        self._proc = proc
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._stopped = False
        self._thread.start()

    def _reader_loop(self) -> None:
        """Continuously read from PTY and queue chunks."""
        while not self._stopped:
            try:
                data = self._proc.read(4096)
                if data:
                    self._queue.put(data)
                else:
                    self._queue.put(None)  # EOF
                    break
            except EOFError:
                self._queue.put(None)
                break
            except Exception:
                if not self._stopped:
                    self._queue.put(None)
                break

    def read(self) -> str:
        """Return available data without blocking (empty if none)."""
        chunks = []
        while not self._queue.empty():
            try:
                data = self._queue.get_nowait()
                if data is None:
                    break
                chunks.append(data)
            except Exception:
                break
        return "".join(chunks)

    def stop(self) -> None:
        self._stopped = True


# Global map of proc -> reader thread (so we reuse the same reader)
_windows_readers: dict[int, _WindowsReaderThread] = {}


def _read_windows(proc: Any) -> str:
    """Non-blocking read from a pywinpty process.

    Uses a persistent background reader thread since pywinpty's
    read() blocks and per-call threads deadlock each other.
    """
    proc_id = id(proc)
    if proc_id not in _windows_readers:
        _windows_readers[proc_id] = _WindowsReaderThread(proc)
    return _windows_readers[proc_id].read()


def _close_windows(proc: Any, *, force: bool) -> None:
    """Close a pywinpty process and clean up its reader thread."""
    # Stop the reader thread first
    proc_id = id(proc)
    reader = _windows_readers.pop(proc_id, None)
    if reader:
        reader.stop()

    try:
        if force:
            proc.terminate()
        else:
            # Send Ctrl+C then wait briefly
            try:
                proc.write("\x03")
                time.sleep(0.5)
            except Exception:
                pass
            if proc.isalive():
                proc.terminate()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# POSIX backend (ptyprocess)
# ---------------------------------------------------------------------------

def _spawn_posix(
    argv: Sequence[str],
    *,
    cwd: str | None,
    env: dict[str, str],
    cols: int,
    rows: int,
) -> Any:
    """Spawn via ptyprocess."""
    from ptyprocess import PtyProcessUnicode

    proc = PtyProcessUnicode.spawn(
        list(argv),
        cwd=cwd,
        env=env,
        dimensions=(rows, cols),
    )
    return proc


def _read_posix(proc: Any) -> str:
    """Non-blocking read from a ptyprocess."""
    import select
    import errno

    try:
        # Check if data is available (non-blocking)
        if hasattr(proc, 'fd'):
            ready, _, _ = select.select([proc.fd], [], [], 0.1)
            if not ready:
                return ""
        return proc.read(4096)
    except EOFError:
        return ""
    except OSError as exc:
        if exc.errno == errno.EIO:
            return ""  # Process closed its end
        raise


def _close_posix(proc: Any, *, force: bool) -> None:
    """Close a ptyprocess."""
    try:
        if force:
            proc.terminate(force=True)
        else:
            proc.sendeof()
            time.sleep(0.5)
            if proc.isalive():
                proc.terminate()
                time.sleep(0.5)
                if proc.isalive():
                    proc.terminate(force=True)
    except Exception:
        pass
