"""The process runner: the one seam through which every launch passes.

:class:`ProcessRunner` is the contract. :class:`SubprocessRunner` is the real
implementation over ``subprocess``, and :class:`RecordingRunner` is a fake that
records each :class:`~work_buddy.process.policy.ResolvedLaunch` and returns
scripted results, so tests assert the policy a call site received without
starting a process.

:func:`use_runner` installs a runner for the duration of a block. It is a
process-wide override rather than a context variable, so launches made from
threads the code under test starts are captured too.

Standard library only.
"""

from __future__ import annotations

import errno
import io
import itertools
import logging
import os
import signal
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import IO, Any, Callable, Iterator, Protocol, Sequence, Union

from work_buddy.process.policy import (
    CREATE_NO_WINDOW,
    Intent,
    ResolvedLaunch,
    is_windows,
)

logger = logging.getLogger(__name__)


class ExecutableNotFound(FileNotFoundError):
    """The program to launch does not exist.

    A ``FileNotFoundError``, so existing ``except FileNotFoundError`` handlers
    keep working.
    """


class ProcessTimeout(subprocess.TimeoutExpired, TimeoutError):
    """A tool run outlived its timeout, and its process tree was terminated.

    Both a ``subprocess.TimeoutExpired``, so existing handlers keep working,
    and a ``TimeoutError``, so the resilience framework classifies it as a
    timeout. ``output`` and ``stderr`` hold whatever the tool wrote first.
    """

    def __init__(
        self,
        cmd: Sequence[str],
        timeout: float,
        output: str | bytes | None = None,
        stderr: str | bytes | None = None,
    ) -> None:
        subprocess.TimeoutExpired.__init__(self, cmd, timeout, output, stderr)
        self.args = (cmd, timeout)

    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (self.cmd, self.timeout, self.output, self.stderr))


class LaunchFailed(OSError):
    """work-buddy could not start a process for a reason other than a
    missing program, for example no terminal emulator is installed."""


class ProcessResult(subprocess.CompletedProcess):
    """The outcome of a tool run.

    A ``subprocess.CompletedProcess``, so ``returncode``, ``stdout``,
    ``stderr`` and ``check_returncode()`` behave exactly as they do for
    ``subprocess.run``.
    """


class ProcessHandle(Protocol):
    """A started process. ``subprocess.Popen`` satisfies it."""

    pid: int
    returncode: int | None
    stdin: IO[Any] | None
    stdout: IO[Any] | None
    stderr: IO[Any] | None

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def communicate(
        self, input: Any = None, timeout: float | None = None,
    ) -> tuple[Any, Any]: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


class ProcessRunner(Protocol):
    """Executes resolved launches. Implementations add no policy of their own."""

    def run(self, launch: ResolvedLaunch) -> ProcessResult:
        """Start ``launch``, feed it its input, and wait for it.

        Raises :class:`ExecutableNotFound` when the program does not exist,
        and :class:`ProcessTimeout` after terminating the process tree when
        ``launch.timeout`` passes.
        """
        ...

    def spawn(self, launch: ResolvedLaunch) -> ProcessHandle:
        """Start ``launch`` and return without waiting for it.

        Raises :class:`ExecutableNotFound` when the program does not exist.
        """
        ...


# ---------------------------------------------------------------------------
# The real runner
# ---------------------------------------------------------------------------

_REAP_WAIT_S = 5.0
_TASKKILL_TIMEOUT_S = 10.0


def _popen(launch: ResolvedLaunch) -> subprocess.Popen:
    try:
        return subprocess.Popen(list(launch.argv), **launch.popen_kwargs())
    except FileNotFoundError as exc:
        # A missing working directory raises FileNotFoundError too. Report
        # only a missing program as ExecutableNotFound.
        if launch.cwd is not None and not os.path.isdir(launch.cwd):
            raise
        raise ExecutableNotFound(
            exc.errno or errno.ENOENT,
            f"program not found: {launch.argv[0]}",
            launch.argv[0],
        ) from exc


def taskkill_tree(pid: int, *, timeout: float = _TASKKILL_TIMEOUT_S) -> bool:
    """Windows: force-end ``pid`` and every process it started.

    Runs ``taskkill /F /T`` without a window. This is the package's lowest
    layer: it launches directly rather than through a runner, so a hung
    ``taskkill`` can never recurse into another termination. Returns ``True``
    only when ``taskkill`` reported success.
    """
    try:
        completed = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def reap_launched(proc: subprocess.Popen, launch: ResolvedLaunch) -> None:
    """End a process this runner started, with everything it started, then
    wait for it, so no pipe or handle outlives the call."""
    if proc.poll() is None:
        if is_windows():
            if not taskkill_tree(proc.pid):
                try:
                    proc.kill()
                except OSError:
                    pass
        elif launch.start_new_session:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                try:
                    proc.kill()
                except OSError:
                    pass
        else:
            try:
                proc.kill()
            except OSError:
                pass
    try:
        proc.wait(timeout=_REAP_WAIT_S)
    except subprocess.TimeoutExpired:
        logger.warning("process %d did not exit after termination", proc.pid)


def _drain_after_kill(proc: subprocess.Popen) -> tuple[Any, Any]:
    """Collect what a terminated process wrote, without waiting on a
    grandchild that still holds its pipes."""
    try:
        return proc.communicate(timeout=_REAP_WAIT_S)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        return None, None


class SubprocessRunner:
    """The real runner, over ``subprocess.Popen``.

    Tool runs use ``communicate(timeout=...)`` rather than ``subprocess.run``,
    because ``run`` ends only the direct child on timeout. Here a timeout, or
    any exception while waiting such as ``KeyboardInterrupt``, ends the whole
    process tree before the error propagates.
    """

    def run(self, launch: ResolvedLaunch) -> ProcessResult:
        proc = _popen(launch)
        try:
            stdout, stderr = proc.communicate(input=launch.input, timeout=launch.timeout)
        except subprocess.TimeoutExpired:
            reap_launched(proc, launch)
            stdout, stderr = _drain_after_kill(proc)
            raise ProcessTimeout(
                list(launch.argv), launch.timeout or 0.0, stdout, stderr,
            ) from None
        except BaseException:
            reap_launched(proc, launch)
            raise
        return ProcessResult(list(launch.argv), proc.returncode, stdout, stderr)

    def spawn(self, launch: ResolvedLaunch) -> ProcessHandle:
        return _popen(launch)


# ---------------------------------------------------------------------------
# The recording fake
# ---------------------------------------------------------------------------

Matcher = Union[Sequence[str], Callable[[ResolvedLaunch], bool], None]


@dataclass
class Scripted:
    """What the recording runner answers for launches a rule matches."""

    returncode: int = 0
    stdout: str | bytes = ""
    stderr: str | bytes = ""
    raises: BaseException | None = None
    """Raise this instead of returning, for example a ``ProcessTimeout``."""
    running: bool = False
    """``spawn`` only: the fake process stays running until it is ended."""


Responder = Callable[[ResolvedLaunch], Union[Scripted, ProcessResult]]


def _matches(matcher: Matcher, launch: ResolvedLaunch) -> bool:
    if matcher is None:
        return True
    if callable(matcher):
        return bool(matcher(launch))
    prefix = tuple(matcher)
    return launch.argv[: len(prefix)] == prefix


def _as_output(value: str | bytes | None, launch: ResolvedLaunch) -> str | bytes | None:
    """Give scripted output the type the real runner would return."""
    if value is None:
        return None
    if launch.decodes_output:
        return value.decode() if isinstance(value, bytes) else value
    return value.encode() if isinstance(value, str) else value


class FakeProcess:
    """A process handle for :meth:`RecordingRunner.spawn`. Nothing runs.

    A scripted ``running=True`` process stays alive until :meth:`finish`,
    :meth:`terminate` or :meth:`kill` ends it. Otherwise it has already exited
    with its scripted return code.
    """

    _pids = itertools.count(40_000)

    def __init__(self, launch: ResolvedLaunch, script: Scripted) -> None:
        self.launch = launch
        self.args = list(launch.argv)
        self.pid = next(self._pids)
        self._script = script
        self.returncode: int | None = None if script.running else script.returncode
        self.stdin = self._stream(launch.stdin, write=True)
        self.stdout = self._stream(launch.stdout, value=script.stdout)
        self.stderr = self._stream(launch.stderr, value=script.stderr)
        self.terminated = False

    def _stream(self, stdio: Any, *, write: bool = False, value: Any = "") -> Any:
        if stdio != subprocess.PIPE:
            return None
        if self.launch.decodes_output:
            return io.StringIO("" if write else _as_output(value, self.launch))
        return io.BytesIO(b"" if write else _as_output(value, self.launch))

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            if timeout is not None:
                raise subprocess.TimeoutExpired(self.args, timeout)
            self.finish(self._script.returncode)
        return self.returncode  # type: ignore[return-value]

    def communicate(self, input: Any = None, timeout: float | None = None) -> tuple[Any, Any]:
        if input is not None and self.stdin is not None:
            self.stdin.write(input)
        self.wait(timeout)
        out = self.stdout.getvalue() if self.stdout is not None else None
        err = self.stderr.getvalue() if self.stderr is not None else None
        return out, err

    def finish(self, returncode: int = 0) -> None:
        """End the fake process with ``returncode``."""
        if self.returncode is None:
            self.returncode = returncode

    def terminate(self) -> None:
        self.terminated = True
        self.finish(1)

    def kill(self) -> None:
        self.terminated = True
        self.finish(-9)


class RecordingRunner:
    """A fake runner: records every launch and answers from scripted rules.

    ``script`` adds a rule. A rule matches by argv prefix (a sequence of
    strings), by predicate, or everything (``None``). The most recently added
    matching rule answers, so a test can add a general rule first and
    override it for specific commands later. Unmatched launches succeed with
    empty output, or raise ``AssertionError`` when ``strict=True``.
    """

    def __init__(self, *, strict: bool = False) -> None:
        self.strict = strict
        self.launches: list[ResolvedLaunch] = []
        self.processes: list[FakeProcess] = []
        self._rules: list[tuple[Matcher, Scripted | Responder]] = []
        self._lock = threading.Lock()

    def script(
        self,
        match: Matcher = None,
        *,
        returncode: int = 0,
        stdout: str | bytes = "",
        stderr: str | bytes = "",
        raises: BaseException | None = None,
        running: bool = False,
        respond: Responder | None = None,
    ) -> "RecordingRunner":
        """Answer launches that ``match`` with a result, an exception, or
        whatever ``respond(launch)`` returns. Returns ``self`` for chaining."""
        answer: Scripted | Responder = respond or Scripted(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            raises=raises,
            running=running,
        )
        with self._lock:
            self._rules.append((match, answer))
        return self

    def _answer(self, launch: ResolvedLaunch) -> Scripted | ProcessResult:
        with self._lock:
            self.launches.append(launch)
            rules = list(self._rules)
        for matcher, answer in reversed(rules):
            if _matches(matcher, launch):
                return answer(launch) if callable(answer) else answer
        if self.strict:
            raise AssertionError(f"unexpected launch: {list(launch.argv)}")
        return Scripted()

    def run(self, launch: ResolvedLaunch) -> ProcessResult:
        answer = self._answer(launch)
        if isinstance(answer, ProcessResult):
            return answer
        if answer.raises is not None:
            raise answer.raises
        return ProcessResult(
            list(launch.argv),
            answer.returncode,
            _as_output(answer.stdout, launch),
            _as_output(answer.stderr, launch),
        )

    def spawn(self, launch: ResolvedLaunch) -> ProcessHandle:
        answer = self._answer(launch)
        if isinstance(answer, ProcessResult):
            answer = Scripted(
                returncode=answer.returncode,
                stdout=answer.stdout or "",
                stderr=answer.stderr or "",
            )
        if answer.raises is not None:
            raise answer.raises
        process = FakeProcess(launch, answer)
        with self._lock:
            self.processes.append(process)
        return process

    # -- assertions ---------------------------------------------------------

    def argvs(self) -> list[list[str]]:
        """Every recorded argv, in launch order."""
        return [list(launch.argv) for launch in self.launches]

    def launches_of(self, *prefix: str, intent: Intent | None = None) -> list[ResolvedLaunch]:
        """Recorded launches whose argv starts with ``prefix``."""
        return [
            launch for launch in self.launches
            if launch.argv[: len(prefix)] == prefix
            and (intent is None or launch.intent is intent)
        ]


# ---------------------------------------------------------------------------
# The override
# ---------------------------------------------------------------------------

_DEFAULT_RUNNER = SubprocessRunner()
_override_lock = threading.Lock()
_override_stack: list[ProcessRunner] = []


def current_runner() -> ProcessRunner:
    """The runner launches go through: the innermost :func:`use_runner`
    override, or the real runner."""
    with _override_lock:
        return _override_stack[-1] if _override_stack else _DEFAULT_RUNNER


@contextmanager
def use_runner(runner: ProcessRunner) -> Iterator[ProcessRunner]:
    """Send every launch in this process through ``runner`` for the block.

    For tests. Overrides nest, and each block restores the runner that was
    in place before it.
    """
    with _override_lock:
        _override_stack.append(runner)
    try:
        yield runner
    finally:
        with _override_lock:
            _override_stack.remove(runner)
