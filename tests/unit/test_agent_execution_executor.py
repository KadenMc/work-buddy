from __future__ import annotations

import threading
from pathlib import Path

import pytest

from work_buddy.sidecar.dispatch import executor


class _Input:
    def __init__(self) -> None:
        self.value = ""
        self.closed = False

    def write(self, value: str) -> None:
        self.value += value

    def close(self) -> None:
        self.closed = True


class _Process:
    def __init__(self, pid: int = 123, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode
        self.stdin = _Input()
        self.killed = False

    def kill(self) -> None:
        self.killed = True

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.killed = True


class _TerminableProcess(_Process):
    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise executor.subprocess.TimeoutExpired(
                cmd="detached-test-process",
                timeout=timeout,
            )
        return self.returncode


class _WaitableProcess(_Process):
    def __init__(self, pid: int = 123) -> None:
        super().__init__(pid=pid)
        self.exited = threading.Event()

    def wait(self) -> int:
        self.exited.wait(1)
        self.returncode = 0
        return 0


@pytest.fixture(autouse=True)
def _clear_owned_processes():
    executor._OWNED_DETACHED_PROCESSES.clear()
    yield
    executor._OWNED_DETACHED_PROCESSES.clear()


def test_generic_detached_spawn_uses_fixed_argv_no_shell_and_stdin(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    process = _Process()

    def popen(command: list[str], **kwargs: object) -> _Process:
        captured["command"] = command
        captured.update(kwargs)
        return process

    monkeypatch.setattr(executor.subprocess, "Popen", popen)
    result = executor._spawn_detached_process_unchecked(
        name="codex-worker",
        argv=["python", "-m", "worker", "--model", "exact"],
        cwd=tmp_path,
        env={"SAFE": "yes"},
        stdin_text="private prompt",
        session_name="daemon:codex-worker",
    )

    assert result == {
        "status": "ok",
        "pid": 123,
        "session_name": "daemon:codex-worker",
        "process_owner": "daemon:codex-worker",
    }
    assert captured["command"] == [
        "python",
        "-m",
        "worker",
        "--model",
        "exact",
    ]
    assert captured["shell"] is False
    assert captured["start_new_session"] is (executor.os.name != "nt")
    assert captured["cwd"] == str(tmp_path)
    assert captured["env"] == {"SAFE": "yes"}
    assert process.stdin.value == "private prompt"
    assert process.stdin.closed
    assert (
        executor._owned_detached_process(123, "daemon:codex-worker")
        is process
    )


def test_generic_detached_spawn_bounds_stdin_delivery(
    monkeypatch,
    tmp_path: Path,
) -> None:
    release = threading.Event()
    process = _Process(pid=456)
    terminated: list[int] = []

    def blocked_write(_value: str) -> None:
        release.wait(1)

    process.stdin.write = blocked_write
    monkeypatch.setattr(
        executor.subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )
    monkeypatch.setattr(
        executor,
        "_DETACHED_STDIN_TIMEOUT_SECONDS",
        0.01,
    )

    def terminate(proc: _Process, owner_token: str) -> bool:
        terminated.append(proc.pid)
        executor._forget_owned_detached_process(
            proc.pid,
            owner_token,
            proc,
        )
        return True

    monkeypatch.setattr(executor, "_terminate_owned_process_handle", terminate)
    result = executor._spawn_detached_process_unchecked(
        name="blocked-worker",
        argv=["python", "-m", "worker"],
        cwd=tmp_path,
        stdin_text="private prompt",
        session_name="blocked-owner",
    )
    release.set()

    assert result == {
        "status": "error",
        "error_code": "spawn_input_timeout",
        "error": "Agent process could not start.",
    }
    assert terminated == [456]
    assert executor._owned_detached_process(456, "blocked-owner") is None


def test_failed_stdin_delivery_keeps_reaper_and_cleanup_retry(
    monkeypatch,
    tmp_path: Path,
) -> None:
    release = threading.Event()
    process = _Process(pid=457)
    reaped: list[tuple[int, str]] = []
    retried: list[tuple[int, str]] = []

    def blocked_write(_value: str) -> None:
        release.wait(1)

    process.stdin.write = blocked_write
    monkeypatch.setattr(
        executor.subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )
    monkeypatch.setattr(
        executor,
        "_DETACHED_STDIN_TIMEOUT_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        executor,
        "_start_detached_process_reaper",
        lambda proc, owner: reaped.append((proc.pid, owner)),
    )
    monkeypatch.setattr(
        executor,
        "_terminate_owned_process_handle",
        lambda _proc, _owner: False,
    )
    monkeypatch.setattr(
        executor,
        "_start_detached_termination_retrier",
        lambda proc, owner: retried.append((proc.pid, owner)),
    )

    result = executor._spawn_detached_process_unchecked(
        name="blocked-worker",
        argv=["python", "-m", "worker"],
        cwd=tmp_path,
        stdin_text="private prompt",
        session_name="blocked-owner",
    )
    release.set()

    assert result["status"] == "error"
    assert result["error_code"] == "spawn_input_timeout"
    assert reaped == [(457, "blocked-owner")]
    assert retried == [(457, "blocked-owner")]
    assert (
        executor._owned_detached_process(457, "blocked-owner")
        is process
    )


def test_generic_detached_spawn_returns_safe_missing_runtime_error(
    monkeypatch,
    tmp_path: Path,
) -> None:
    def popen(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError("private executable path")

    monkeypatch.setattr(executor.subprocess, "Popen", popen)
    result = executor._spawn_detached_process_unchecked(
        name="worker",
        argv=["missing-runtime"],
        cwd=tmp_path,
        missing_executable_error="Provider is not installed.",
    )

    assert result == {
        "status": "error",
        "error_code": "runtime_not_installed",
        "error": "Provider is not installed.",
    }


def test_claude_headless_argv_uses_caller_selected_model() -> None:
    argv = executor._build_headless_agent_argv(
        prompt="brief",
        session_name="daemon:document",
        model="opus",
        max_budget_usd=2.0,
        persistent=True,
    )

    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[-1] == "brief"


def test_terminate_detached_process_validates_pid_and_uses_windows_tree_kill(
    monkeypatch,
) -> None:
    killed: list[int] = []
    process = _TerminableProcess(pid=9876)
    executor._OWNED_DETACHED_PROCESSES[(process.pid, "generation-a")] = process
    monkeypatch.setattr(executor.os, "name", "nt")

    def _kill(pid: int) -> bool:
        killed.append(pid)
        process.returncode = -9
        return True

    monkeypatch.setattr(
        "work_buddy.compat._force_kill_pid",
        _kill,
    )

    assert executor.terminate_detached_process(
        True,
        owner_token="generation-a",
    ) is False
    assert executor.terminate_detached_process(
        0,
        owner_token="generation-a",
    ) is False
    assert executor.terminate_detached_process(
        -4,
        owner_token="generation-a",
    ) is False
    assert executor.terminate_detached_process(
        9876,
        owner_token="generation-a",
    ) is True
    assert killed == [9876]
    assert executor._owned_detached_process(9876, "generation-a") is None


def test_terminate_detached_process_refuses_live_unowned_pid(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "work_buddy.utils.process.is_process_alive",
        lambda pid: True,
    )
    assert executor.terminate_detached_process(
        9876,
        owner_token="generation-a",
    ) is False


def test_terminate_detached_process_treats_gone_process_as_stopped(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "work_buddy.utils.process.is_process_alive",
        lambda pid: False,
    )
    assert executor.terminate_detached_process(
        9876,
        owner_token="generation-a",
    ) is True


def test_natural_process_exit_releases_only_its_owned_handle() -> None:
    process = _WaitableProcess(pid=9876)
    replacement = _Process(pid=9876)
    executor._OWNED_DETACHED_PROCESSES[(9876, "generation-old")] = process
    executor._OWNED_DETACHED_PROCESSES[(9876, "generation-new")] = replacement

    reaper = executor._start_detached_process_reaper(
        process,
        "generation-old",
    )
    assert reaper is not None
    process.exited.set()
    reaper.join(timeout=1)

    assert not reaper.is_alive()
    assert executor._owned_detached_process(9876, "generation-old") is None
    assert (
        executor._owned_detached_process(9876, "generation-new")
        is replacement
    )


def test_failed_termination_retains_owned_handle_for_a_retry(
    monkeypatch,
) -> None:
    process = _Process(pid=9876)
    executor._OWNED_DETACHED_PROCESSES[(9876, "generation-a")] = process
    monkeypatch.setattr(executor.os, "name", "nt")

    def _fail_to_kill(_pid: int) -> None:
        raise OSError("access denied")

    monkeypatch.setattr("work_buddy.compat._force_kill_pid", _fail_to_kill)

    assert executor.terminate_detached_process(
        9876,
        owner_token="generation-a",
    ) is False
    assert (
        executor._owned_detached_process(9876, "generation-a")
        is process
    )


def test_taskkill_nonzero_retains_owned_handle_for_a_retry(
    monkeypatch,
) -> None:
    from work_buddy import compat

    process = _Process(pid=9876)
    executor._OWNED_DETACHED_PROCESSES[(9876, "generation-a")] = process
    monkeypatch.setattr(executor.os, "name", "nt")
    monkeypatch.setattr(compat, "IS_WINDOWS", True)

    class _TaskkillFailure:
        returncode = 5

    monkeypatch.setattr(
        compat.subprocess,
        "run",
        lambda *_args, **_kwargs: _TaskkillFailure(),
    )

    assert executor.terminate_detached_process(
        9876,
        owner_token="generation-a",
    ) is False
    assert (
        executor._owned_detached_process(9876, "generation-a")
        is process
    )


def test_taskkill_success_without_observed_exit_retains_owned_handle(
    monkeypatch,
) -> None:
    process = _TerminableProcess(pid=9876)
    executor._OWNED_DETACHED_PROCESSES[(9876, "generation-a")] = process
    monkeypatch.setattr(executor.os, "name", "nt")
    monkeypatch.setattr(
        "work_buddy.compat._force_kill_pid",
        lambda _pid: True,
    )

    assert executor.terminate_detached_process(
        9876,
        owner_token="generation-a",
    ) is False
    assert (
        executor._owned_detached_process(9876, "generation-a")
        is process
    )


def test_unix_signal_without_observed_exit_retains_owned_handle(
    monkeypatch,
) -> None:
    process = _TerminableProcess(pid=9876)
    executor._OWNED_DETACHED_PROCESSES[(9876, "generation-a")] = process
    signalled: list[tuple[int, int]] = []
    # os is the process-global module. Restore os.name before pytest constructs
    # any Windows pathlib objects for reporting and cache cleanup.
    with monkeypatch.context() as platform:
        platform.setattr(executor.os, "name", "posix")
        platform.setattr(
            executor.os,
            "killpg",
            lambda pid, sig: signalled.append((pid, sig)),
            raising=False,
        )
        assert executor.terminate_detached_process(
            9876,
            owner_token="generation-a",
        ) is False
    assert signalled == [(9876, executor.signal.SIGTERM)]
    assert (
        executor._owned_detached_process(9876, "generation-a")
        is process
    )


def test_stale_owner_token_cannot_kill_reused_pid(
    monkeypatch,
) -> None:
    killed: list[int] = []
    exited = _Process(pid=9876, returncode=0)
    replacement = _TerminableProcess(pid=9876)
    executor._OWNED_DETACHED_PROCESSES[(9876, "generation-old")] = exited
    executor._OWNED_DETACHED_PROCESSES[(9876, "generation-new")] = replacement
    monkeypatch.setattr(executor.os, "name", "nt")

    def _kill(pid: int) -> bool:
        killed.append(pid)
        replacement.returncode = -9
        return True

    monkeypatch.setattr(
        "work_buddy.compat._force_kill_pid",
        _kill,
    )

    assert executor.terminate_detached_process(
        9876,
        owner_token="generation-old",
    ) is True
    assert killed == []
    assert (
        executor._owned_detached_process(9876, "generation-new")
        is replacement
    )

    assert executor.terminate_detached_process(
        9876,
        owner_token="generation-new",
    ) is True
    assert killed == [9876]
