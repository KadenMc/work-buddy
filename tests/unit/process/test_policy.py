"""The launch policy table, evaluated for Windows and POSIX on any platform."""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path

import pytest

from work_buddy.process import policy
from work_buddy.process.policy import (
    CREATE_NEW_CONSOLE,
    CREATE_NO_WINDOW,
    NON_INTERACTIVE_ENV,
    Intent,
    ToolRun,
    WorkerSpec,
)

# Windows' DETACHED_PROCESS. No intent may use it: a process with no console
# makes Windows open a visible window for every console child it starts.
DETACHED_PROCESS = 0x00000008

WINDOWS = "win32"
POSIX = "linux"


def _tool(**overrides) -> ToolRun:
    fields = {"argv": ("git", "status"), "timeout": 5.0}
    fields.update(overrides)
    return ToolRun(**fields)


# -- tool runs --------------------------------------------------------------


def test_tool_run_from_a_host_with_no_console_gets_one_without_a_window() -> None:
    launch = policy.resolve_tool(
        _tool(), platform=WINDOWS, base_env={}, deadline_remaining=None, has_console=False,
    )

    assert launch.intent is Intent.TOOL
    assert launch.creationflags == CREATE_NO_WINDOW
    assert not launch.creationflags & DETACHED_PROCESS
    assert launch.start_new_session is False
    assert launch.stdin == subprocess.DEVNULL
    assert launch.stdout == subprocess.PIPE
    assert launch.stderr == subprocess.PIPE
    assert launch.env == NON_INTERACTIVE_ENV


def test_tool_run_from_a_host_with_a_console_shares_it() -> None:
    """A console child inherits its parent's console, which has no window
    (a background host) or is a terminal already open (the CLI), so neither
    opens a window, and sharing it saves a console host per call."""
    launch = policy.resolve_tool(
        _tool(), platform=WINDOWS, base_env={}, deadline_remaining=None, has_console=True,
    )

    assert launch.creationflags == 0
    assert not launch.creationflags & (CREATE_NEW_CONSOLE | DETACHED_PROCESS)


def test_a_simulated_windows_counts_as_having_no_console(monkeypatch) -> None:
    monkeypatch.setattr(policy.sys, "platform", "linux")

    launch = policy.resolve_tool(_tool(), platform=WINDOWS, base_env={}, deadline_remaining=None)

    assert launch.creationflags == CREATE_NO_WINDOW


@pytest.mark.parametrize(
    ("foreground", "new_session"),
    [(False, True), (True, False)],
    ids=["background", "terminal-foreground"],
)
def test_tool_run_on_posix_gets_its_own_session_unless_in_the_foreground(
    foreground: bool, new_session: bool,
) -> None:
    launch = policy.resolve_tool(
        _tool(), platform=POSIX, base_env={}, foreground_terminal=foreground,
        deadline_remaining=None,
    )

    assert launch.creationflags == 0
    assert launch.start_new_session is new_session


def test_tool_run_env_overlay_applies_on_top_of_inherited_and_supplied_env() -> None:
    inherited = policy.resolve_tool(
        _tool(), platform=POSIX, base_env={"PATH": "/bin", "GIT_TERMINAL_PROMPT": "1"},
        foreground_terminal=False, deadline_remaining=None,
    )
    supplied = policy.resolve_tool(
        _tool(env={"HOME": "/tmp"}), platform=POSIX, base_env={"PATH": "/bin"},
        foreground_terminal=False, deadline_remaining=None,
    )

    assert inherited.env == {"PATH": "/bin", **NON_INTERACTIVE_ENV}
    # A supplied environment replaces the inherited one, as with subprocess.
    assert supplied.env == {"HOME": "/tmp", **NON_INTERACTIVE_ENV}


def test_tool_run_input_opens_stdin() -> None:
    launch = policy.resolve_tool(
        _tool(input="payload"), platform=POSIX, base_env={}, foreground_terminal=False,
        deadline_remaining=None,
    )

    assert launch.stdin == subprocess.PIPE
    assert launch.input == "payload"


def test_tool_run_decodes_like_subprocess_run_text_true_by_default() -> None:
    launch = policy.resolve_tool(_tool(), platform=POSIX, base_env={}, deadline_remaining=None,
                                 foreground_terminal=False)

    kwargs = launch.popen_kwargs()
    assert kwargs["text"] is True
    assert kwargs["encoding"] is None
    assert kwargs["errors"] is None


def test_tool_run_bytes_mode_passes_no_text_arguments() -> None:
    launch = policy.resolve_tool(
        _tool(text=False), platform=POSIX, base_env={}, foreground_terminal=False,
        deadline_remaining=None,
    )

    kwargs = launch.popen_kwargs()
    assert "text" not in kwargs and "encoding" not in kwargs and "errors" not in kwargs


def test_tool_run_explicit_decoding_is_kept() -> None:
    launch = policy.resolve_tool(
        _tool(text=False, encoding="utf-8", errors="replace"), platform=POSIX, base_env={},
        foreground_terminal=False, deadline_remaining=None,
    )

    kwargs = launch.popen_kwargs()
    assert (kwargs["text"], kwargs["encoding"], kwargs["errors"]) == (True, "utf-8", "replace")


@pytest.mark.parametrize("timeout", [None, 0, -1, float("inf"), float("nan"), True, "5"])
def test_tool_run_requires_a_finite_positive_timeout(timeout) -> None:
    with pytest.raises((TypeError, ValueError)):
        _tool(timeout=timeout)


@pytest.mark.parametrize(
    ("remaining", "effective"),
    [(None, 5.0), (2.5, 2.5), (60.0, 5.0), (-1.0, 0.0)],
    ids=["no-deadline", "deadline-shorter", "deadline-longer", "deadline-passed"],
)
def test_tool_run_timeout_is_clamped_to_the_caller_deadline(remaining, effective) -> None:
    launch = policy.resolve_tool(
        _tool(timeout=5.0), platform=POSIX, base_env={}, foreground_terminal=False,
        deadline_remaining=remaining,
    )

    assert launch.timeout == effective


def test_ambient_deadline_comes_from_a_bound_resilience_context() -> None:
    from work_buddy.resilience import Deadline, ResilienceContext, use_context

    assert policy.ambient_deadline_remaining() is None
    context = ResilienceContext(operation_key="test", deadline=Deadline.after(3.0))
    with use_context(context):
        remaining = policy.ambient_deadline_remaining()
    assert remaining is not None and 0 < remaining <= 3.0
    unbounded = ResilienceContext(operation_key="test", deadline=Deadline.never())
    with use_context(unbounded):
        assert policy.ambient_deadline_remaining() is None


def test_argv_is_normalized_and_validated(tmp_path: Path) -> None:
    assert _tool(argv=["git", tmp_path]).argv == ("git", str(tmp_path))
    with pytest.raises(TypeError):
        _tool(argv="git status")
    with pytest.raises(ValueError):
        _tool(argv=[])
    with pytest.raises(TypeError):
        _tool(argv=["git", 3])
    with pytest.raises(ValueError):
        _tool(argv=["git", "a\x00b"])


def test_launches_never_use_a_shell() -> None:
    launch = policy.resolve_tool(_tool(), platform=POSIX, base_env={}, foreground_terminal=False,
                                 deadline_remaining=None)

    assert launch.popen_kwargs()["shell"] is False


# -- workers ------------------------------------------------------------------


def test_worker_on_windows_has_no_window_and_no_timeout() -> None:
    launch = policy.resolve_worker(WorkerSpec(argv=("node", "kernel.js")), platform=WINDOWS)

    assert launch.intent is Intent.WORKER
    assert launch.creationflags == CREATE_NO_WINDOW
    assert launch.timeout is None
    assert launch.stdin == launch.stdout == launch.stderr == subprocess.DEVNULL
    assert launch.env is None


@pytest.mark.parametrize("new_session", [False, True])
def test_worker_on_posix_starts_a_session_only_on_request(new_session: bool) -> None:
    launch = policy.resolve_worker(
        WorkerSpec(argv=("claude",), new_session=new_session), platform=POSIX,
    )

    assert launch.creationflags == 0
    assert launch.start_new_session is new_session


def test_worker_pipes_and_decoding_pass_through() -> None:
    spec = WorkerSpec(
        argv=("node",), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        text=True, encoding="utf-8", bufsize=1, env={"A": "1"},
    )

    launch = policy.resolve_worker(spec, platform=WINDOWS)

    assert (launch.stdin, launch.stdout) == (subprocess.PIPE, subprocess.PIPE)
    assert launch.popen_kwargs()["bufsize"] == 1
    assert launch.popen_kwargs()["encoding"] == "utf-8"
    assert launch.env == {"A": "1"}


# -- hosts and visible terminals ---------------------------------------------------


def test_host_on_windows_gets_a_hidden_console_not_a_detached_process() -> None:
    launch = policy.resolve_host_launch(
        ["python.exe", "-m", "work_buddy.sidecar"], cwd=None, env={}, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, detached=True, platform=WINDOWS,
    )

    assert launch.intent is Intent.HOST
    assert launch.creationflags == CREATE_NO_WINDOW
    assert not launch.creationflags & DETACHED_PROCESS
    assert launch.start_new_session is False
    assert launch.stdin == subprocess.DEVNULL


@pytest.mark.parametrize("detached", [False, True])
def test_host_on_posix_detaches_by_session(detached: bool) -> None:
    launch = policy.resolve_host_launch(
        ["python", "-m", "x"], cwd="/", env={}, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, detached=detached, platform=POSIX,
    )

    assert launch.creationflags == 0
    assert launch.start_new_session is detached


def test_visible_terminal_is_the_only_intent_that_opens_a_window() -> None:
    windows = policy.resolve_visible_terminal(["pwsh", "-NoExit"], platform=WINDOWS)
    posix = policy.resolve_visible_terminal(["xterm"], platform=POSIX)

    assert windows.intent is Intent.VISIBLE_TERMINAL
    assert windows.creationflags == CREATE_NEW_CONSOLE
    assert windows.stdin is windows.stdout is windows.stderr is None
    assert posix.creationflags == 0
    assert posix.stdin == posix.stdout == posix.stderr == subprocess.DEVNULL
    for launch in (
        policy.resolve_tool(
            _tool(), platform=WINDOWS, base_env={}, deadline_remaining=None, has_console=False,
        ),
        policy.resolve_worker(WorkerSpec(argv=("x",)), platform=WINDOWS),
        policy.resolve_host_launch(["x"], cwd=None, env={}, stdout=None, stderr=None,
                                   detached=True, platform=WINDOWS),
    ):
        assert not launch.creationflags & CREATE_NEW_CONSOLE


# -- PowerShell ----------------------------------------------------------------------


def test_powershell_argv_is_always_non_interactive_without_a_profile() -> None:
    argv = policy.powershell_argv("Get-Date")

    assert argv[0] == "powershell.exe"
    assert "-NoProfile" in argv and "-NonInteractive" in argv
    assert argv[-2:] == ["-Command", "Get-Date"]


def test_powershell_argv_can_encode_the_script() -> None:
    script = "Write-Output 'it''s'"

    argv = policy.powershell_argv(script, encode=True, executable="pwsh")

    assert argv[0] == "pwsh"
    assert argv[-2] == "-EncodedCommand"
    assert base64.b64decode(argv[-1]).decode("utf-16-le") == script
