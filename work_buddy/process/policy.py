"""The launch policy: how each intent starts a process on each platform.

This module is the single place that decides operating-system launch
behaviour. Callers say what they are launching (an intent), and the policy
turns that into concrete ``Popen`` arguments (a :class:`ResolvedLaunch`):

| Intent | Windows | POSIX | stdin | Environment | Timeout |
|---|---|---|---|---|---|
| Tool run | ``CREATE_NO_WINDOW`` | new session, except in a terminal's foreground job | ``DEVNULL`` unless input is given | inherited or supplied, plus the non-interactive overlay | required, clamped to the caller's deadline |
| Detached worker | ``CREATE_NO_WINDOW`` | new session on request | ``DEVNULL`` unless a pipe is requested | supplied or inherited | none, the owner terminates it |
| Owned host | ``CREATE_NO_WINDOW`` (see ``host.py``) | new session for detached starts | ``DEVNULL`` | per host role | none |
| Visible terminal | ``CREATE_NEW_CONSOLE`` | the platform terminal emulator | the new terminal's | supplied | none |

Every function takes the platform as a parameter, so the whole table can be
evaluated and tested on any operating system. Standard library only.
"""

from __future__ import annotations

import base64
import enum
import math
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import IO, Any, Mapping, Sequence, Union

# Windows process-creation flags, by value, so the policy can be evaluated
# and tested on POSIX too, where ``subprocess`` does not define them.
CREATE_NEW_CONSOLE = 0x00000010
CREATE_NO_WINDOW = 0x08000000

# Environment that keeps background tool runs from waiting on a prompt that
# nobody will ever see. Applied on top of the inherited or supplied
# environment for every tool run.
NON_INTERACTIVE_ENV: Mapping[str, str] = {
    # git: never prompt for credentials on a terminal.
    "GIT_TERMINAL_PROMPT": "0",
    # Git Credential Manager: never open a sign-in dialog.
    "GCM_INTERACTIVE": "never",
    # git: skip optional index refreshes, so a background status never takes
    # the lock a user's own git command needs.
    "GIT_OPTIONAL_LOCKS": "0",
    # gh: never prompt.
    "GH_PROMPT_DISABLED": "1",
}

Stdio = Union[int, IO[Any], None]
"""A ``Popen`` stdio value: ``DEVNULL``, ``PIPE``, ``STDOUT``, a file, or ``None``."""


class Intent(str, enum.Enum):
    """What a launch is for. The policy derives every launch detail from it."""

    TOOL = "tool"
    """A captured, awaited program: git, gh, netstat, PowerShell, a runner."""

    WORKER = "worker"
    """A child the caller owns and does not await: an agent worker, a helper."""

    HOST = "host"
    """One of work-buddy's own long-running Python processes."""

    VISIBLE_TERMINAL = "visible_terminal"
    """A terminal window the user is meant to see and use."""


def is_windows(platform: str | None = None) -> bool:
    return (platform or sys.platform) == "win32"


@dataclass(frozen=True)
class ResolvedLaunch:
    """Every ``Popen`` argument for one launch, as the policy decided them.

    Runners execute a resolved launch without adding to it, and the recording
    runner keeps each one, so tests can assert the policy that a call site
    received rather than the call site's arguments.
    """

    intent: Intent
    argv: tuple[str, ...]
    cwd: str | None = None
    env: Mapping[str, str] | None = None
    """The complete child environment, or ``None`` to inherit this process's."""
    stdin: Stdio = subprocess.DEVNULL
    stdout: Stdio = subprocess.DEVNULL
    stderr: Stdio = subprocess.DEVNULL
    text: bool = False
    encoding: str | None = None
    errors: str | None = None
    bufsize: int = -1
    creationflags: int = 0
    start_new_session: bool = False
    timeout: float | None = None
    """Tool runs only: the effective timeout after deadline clamping."""
    input: str | bytes | None = None
    """Tool runs only: data written to the child's stdin."""

    def popen_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for ``subprocess.Popen(list(self.argv), ...)``."""
        kwargs: dict[str, Any] = {
            "cwd": self.cwd,
            "env": dict(self.env) if self.env is not None else None,
            "stdin": self.stdin,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "bufsize": self.bufsize,
            "close_fds": True,
            "shell": False,
        }
        if self.text or self.encoding is not None or self.errors is not None:
            kwargs["text"] = True
            kwargs["encoding"] = self.encoding
            kwargs["errors"] = self.errors
        if self.creationflags:
            kwargs["creationflags"] = self.creationflags
        if self.start_new_session:
            kwargs["start_new_session"] = True
        return kwargs

    @property
    def decodes_output(self) -> bool:
        return self.text or self.encoding is not None or self.errors is not None


def normalize_argv(argv: Sequence[str | os.PathLike[str]]) -> tuple[str, ...]:
    """Validate and normalize an argv: a non-empty sequence of strings.

    Path objects are converted with ``os.fspath``. A bare string is rejected,
    because it would be split into characters rather than run as a command:
    tool runs never go through a shell.
    """
    if isinstance(argv, (str, bytes)):
        raise TypeError("argv must be a sequence of arguments, not a single string")
    normalized = tuple(os.fspath(arg) for arg in argv)
    if not normalized:
        raise ValueError("argv must name a program to run")
    for arg in normalized:
        if not isinstance(arg, str):
            raise TypeError(f"argv entries must be strings, got {type(arg).__name__}")
        if "\x00" in arg:
            raise ValueError("argv entries must not contain NUL characters")
    return normalized


def _normalize_cwd(cwd: str | os.PathLike[str] | None) -> str | None:
    return None if cwd is None else os.fspath(cwd)


@dataclass(frozen=True)
class ToolRun:
    """A captured, awaited program run.

    ``timeout`` is required: a tool run that can hang forever is exactly the
    failure this package exists to prevent. ``text``, ``encoding`` and
    ``errors`` default to how ``subprocess.run(..., text=True)`` decodes, so a
    migrated call site reads its tool's output exactly as before.
    """

    argv: tuple[str, ...]
    timeout: float
    cwd: str | None = None
    env: Mapping[str, str] | None = None
    """A complete environment for the child. ``None`` inherits this process's."""
    input: str | bytes | None = None
    text: bool = True
    encoding: str | None = None
    errors: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "argv", normalize_argv(self.argv))
        object.__setattr__(self, "cwd", _normalize_cwd(self.cwd))
        timeout = self.timeout
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise TypeError("a tool run needs a timeout in seconds")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError(
                f"a tool run needs a finite, positive timeout, got {timeout!r}"
            )


@dataclass(frozen=True)
class WorkerSpec:
    """A child process the caller owns and does not await.

    Workers have no timeout: their owner decides when they end and calls
    :func:`work_buddy.process.terminate_tree` to end them.
    """

    argv: tuple[str, ...]
    cwd: str | None = None
    env: Mapping[str, str] | None = None
    stdin: Stdio = subprocess.DEVNULL
    stdout: Stdio = subprocess.DEVNULL
    stderr: Stdio = subprocess.DEVNULL
    text: bool = False
    encoding: str | None = None
    errors: str | None = None
    bufsize: int = -1
    new_session: bool = False
    """POSIX: start the worker in its own session, so terminal signals aimed
    at this process's group do not reach it."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "argv", normalize_argv(self.argv))
        object.__setattr__(self, "cwd", _normalize_cwd(self.cwd))


def in_terminal_foreground() -> bool:
    """True when this process is the foreground job of a terminal.

    That is the one situation where a tool run should share this process's
    group: the user's Ctrl+C then reaches the tool directly. Everywhere else,
    including every background host, a tool run gets its own session.
    """
    if is_windows():
        return False
    stream = sys.stdin
    if stream is None:
        return False
    try:
        fd = stream.fileno()
        return os.isatty(fd) and os.tcgetpgrp(fd) == os.getpgrp()
    except (AttributeError, OSError, ValueError):
        return False


def ambient_deadline_remaining() -> float | None:
    """Seconds left on the caller's resilience deadline, if one is bound.

    A deadline can only be bound through ``work_buddy.resilience``, so when
    that module has never been imported there is nothing to honour, and this
    returns ``None`` without importing it. That keeps tool runs on hot paths
    free of the import.
    """
    context_module = sys.modules.get("work_buddy.resilience.context")
    if context_module is None:
        return None
    context = context_module.current_context()
    if context is None:
        return None
    remaining = context.deadline.remaining()
    return None if remaining == math.inf else remaining


def clamp_timeout(timeout: float, remaining: float | None) -> float:
    """The effective tool-run timeout: the smaller of the two, floored at 0."""
    if remaining is None:
        return timeout
    return max(0.0, min(timeout, remaining))


def resolve_tool(
    spec: ToolRun,
    *,
    platform: str | None = None,
    base_env: Mapping[str, str] | None = None,
    foreground_terminal: bool | None = None,
    deadline_remaining: float | None = None,
) -> ResolvedLaunch:
    """Apply the tool-run policy.

    ``platform``, ``base_env``, ``foreground_terminal`` and
    ``deadline_remaining`` exist for tests. Left out, they come from this
    process: ``sys.platform``, ``os.environ``, :func:`in_terminal_foreground`
    and :func:`ambient_deadline_remaining`.
    """
    windows = is_windows(platform)
    source = spec.env if spec.env is not None else (
        base_env if base_env is not None else os.environ
    )
    env = {**source, **NON_INTERACTIVE_ENV}
    if foreground_terminal is None:
        foreground_terminal = False if windows else in_terminal_foreground()
    if deadline_remaining is None:
        deadline_remaining = ambient_deadline_remaining()
    return ResolvedLaunch(
        intent=Intent.TOOL,
        argv=spec.argv,
        cwd=spec.cwd,
        env=env,
        stdin=subprocess.PIPE if spec.input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=spec.text,
        encoding=spec.encoding,
        errors=spec.errors,
        creationflags=CREATE_NO_WINDOW if windows else 0,
        start_new_session=not windows and not foreground_terminal,
        timeout=clamp_timeout(float(spec.timeout), deadline_remaining),
        input=spec.input,
    )


def resolve_worker(spec: WorkerSpec, *, platform: str | None = None) -> ResolvedLaunch:
    """Apply the detached-worker policy."""
    windows = is_windows(platform)
    return ResolvedLaunch(
        intent=Intent.WORKER,
        argv=spec.argv,
        cwd=spec.cwd,
        env=spec.env,
        stdin=spec.stdin,
        stdout=spec.stdout,
        stderr=spec.stderr,
        text=spec.text,
        encoding=spec.encoding,
        errors=spec.errors,
        bufsize=spec.bufsize,
        creationflags=CREATE_NO_WINDOW if windows else 0,
        start_new_session=spec.new_session and not windows,
    )


def resolve_host_launch(
    argv: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None,
    env: Mapping[str, str],
    stdout: Stdio,
    stderr: Stdio,
    detached: bool,
    platform: str | None = None,
) -> ResolvedLaunch:
    """Apply the owned-host policy. ``host.start_host`` decides the rest.

    On Windows every host gets ``CREATE_NO_WINDOW``. For a console image that
    means a console of its own with no window, which the host's console
    children inherit, so they stay windowless too. A GUI image ignores it.
    """
    windows = is_windows(platform)
    return ResolvedLaunch(
        intent=Intent.HOST,
        argv=normalize_argv(argv),
        cwd=_normalize_cwd(cwd),
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        creationflags=CREATE_NO_WINDOW if windows else 0,
        start_new_session=detached and not windows,
    )


def resolve_visible_terminal(
    argv: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> ResolvedLaunch:
    """Apply the visible-terminal policy.

    On Windows the program gets a new console window of its own, and its
    stdio stays attached to that window. On POSIX ``argv`` already starts a
    terminal emulator, which opens its own window, so this process's stdio
    is not handed to it.
    """
    windows = is_windows(platform)
    stdio: Stdio = None if windows else subprocess.DEVNULL
    return ResolvedLaunch(
        intent=Intent.VISIBLE_TERMINAL,
        argv=normalize_argv(argv),
        cwd=_normalize_cwd(cwd),
        env=env,
        stdin=stdio,
        stdout=stdio,
        stderr=stdio,
        creationflags=CREATE_NEW_CONSOLE if windows else 0,
    )


def powershell_argv(
    script: str,
    *,
    executable: str = "powershell.exe",
    encode: bool = False,
) -> list[str]:
    """The argv for a non-interactive Windows PowerShell run of ``script``.

    Always passes ``-NoProfile`` (a profile can prompt, print, or take
    seconds to load) and ``-NonInteractive`` (a script that would prompt
    fails instead of waiting). ``encode=True`` passes the script as
    ``-EncodedCommand``, which keeps quoting out of the command line.
    """
    argv = [executable, "-NoLogo", "-NoProfile", "-NonInteractive"]
    if encode:
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return [*argv, "-EncodedCommand", encoded]
    return [*argv, "-Command", script]
