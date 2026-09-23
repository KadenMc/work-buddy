"""The intent functions: the sanctioned way to start a process.

Each function builds a specification, lets the policy resolve it, and hands
the resolved launch to the current runner. None of them interprets what the
program printed or what its exit code means: that stays with the caller.

``start_host`` lives in ``host.py``, beside the host roles it depends on.
"""

from __future__ import annotations

import os
import subprocess
from typing import Mapping, Sequence

from work_buddy.process import policy
from work_buddy.process.runner import (
    ProcessHandle,
    ProcessResult,
    ProcessRunner,
    ProcessTimeout,
    current_runner,
)


def run_tool(
    argv: Sequence[str | os.PathLike[str]],
    *,
    timeout: float,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    input: str | bytes | None = None,
    text: bool = True,
    encoding: str | None = None,
    errors: str | None = None,
    check: bool = False,
    runner: ProcessRunner | None = None,
) -> ProcessResult:
    """Run a program to completion and capture its output.

    The program never opens a window and never waits on a prompt: stdin is
    closed unless ``input`` is given, and the environment carries the
    non-interactive overlay (see ``policy.NON_INTERACTIVE_ENV``). ``env``,
    when given, is the child's complete environment, as with
    ``subprocess.run``.

    ``timeout`` is required. When the caller holds a resilience deadline, the
    shorter of the two applies. On timeout the whole process tree is ended
    and :class:`~work_buddy.process.ProcessTimeout` is raised.

    ``text``, ``encoding`` and ``errors`` decode output exactly as they do for
    ``subprocess.run``. ``check=True`` raises ``subprocess.CalledProcessError``
    on a non-zero exit. A missing program raises
    :class:`~work_buddy.process.ExecutableNotFound`, a ``FileNotFoundError``.

    ``runner`` overrides the current runner for this call, for code that
    already receives its runner by injection.
    """
    spec = policy.ToolRun(
        argv=tuple(argv),
        timeout=timeout,
        cwd=cwd,
        env=env,
        input=input,
        text=text,
        encoding=encoding,
        errors=errors,
    )
    launch = policy.resolve_tool(spec)
    if not launch.timeout:
        # The caller's deadline has already passed. Starting a program it can
        # no longer wait for would only leave work behind.
        raise ProcessTimeout(list(launch.argv), 0.0)
    result = (runner or current_runner()).run(launch)
    if check:
        result.check_returncode()
    return result


def spawn_worker(
    argv: Sequence[str | os.PathLike[str]],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    stdin: policy.Stdio = subprocess.DEVNULL,
    stdout: policy.Stdio = subprocess.DEVNULL,
    stderr: policy.Stdio = subprocess.DEVNULL,
    text: bool = False,
    encoding: str | None = None,
    errors: str | None = None,
    bufsize: int = -1,
    new_session: bool = False,
    runner: ProcessRunner | None = None,
) -> ProcessHandle:
    """Start a child process that the caller owns and does not wait for.

    The worker never opens a window. Its stdio defaults to ``DEVNULL``. Pass
    ``subprocess.PIPE`` to talk to it. It has no timeout: the caller ends it
    with :func:`~work_buddy.process.terminate_tree` when it should stop.
    ``new_session=True`` starts it in its own POSIX session, so it survives
    the signals a terminal sends to this process's group.
    """
    spec = policy.WorkerSpec(
        argv=tuple(argv),
        cwd=cwd,
        env=env,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        text=text,
        encoding=encoding,
        errors=errors,
        bufsize=bufsize,
        new_session=new_session,
    )
    return (runner or current_runner()).spawn(policy.resolve_worker(spec))


def open_visible_terminal(
    argv: Sequence[str | os.PathLike[str]],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    runner: ProcessRunner | None = None,
) -> ProcessHandle:
    """Open a terminal window the user is meant to see.

    On Windows, ``argv`` runs in a new console window of its own. On POSIX,
    ``argv`` must itself start a terminal emulator, which opens the window.
    This is the only intent that may open a window.
    """
    launch = policy.resolve_visible_terminal(argv, cwd=cwd, env=env)
    return (runner or current_runner()).spawn(launch)
