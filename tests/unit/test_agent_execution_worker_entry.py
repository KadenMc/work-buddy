"""Detached entry-point diagnostics without launching a model or reading accounts."""

from __future__ import annotations

import io
import json
import logging
import os
import runpy
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from work_buddy import logging_config

_WORKERS = ("claude_worker", "codex_worker")
_SESSION_ID = "diagnostic-generation-assisted-draft"
_PRIVATE_BRIEF = "private-user-draft-not-for-logs — café"
_PRIVATE_ERROR = "private-account-detail-not-for-logs"
_AUTH_FAILURE = (
    "Failed to authenticate: OAuth session expired and could not be refreshed"
)


@pytest.fixture(autouse=True)
def _deny_external_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Worker diagnostic tests must not launch external processes")

    monkeypatch.setattr(subprocess, "Popen", denied)
    monkeypatch.setattr(subprocess, "run", denied)


@pytest.fixture
def worker_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[Path]:
    """Use the real Work Buddy handler hierarchy, isolated from session state.

    Capturing the Python root logger alone would hide the ``__main__`` bug:
    that record reaches pytest but not Work Buddy's configured session file.
    """

    work_buddy_logger = logging.getLogger("work_buddy")
    previous_level = work_buddy_logger.level
    monkeypatch.setattr(work_buddy_logger, "handlers", [])
    monkeypatch.setattr(logging_config, "_configured", False)
    monkeypatch.setattr(logging_config, "_get_log_dir", lambda: tmp_path)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("WORK_BUDDY_SESSION_ID", _SESSION_ID)
    logging_config.setup_logging()
    try:
        yield tmp_path / "work_buddy.log"
    finally:
        for handler in work_buddy_logger.handlers:
            handler.close()
        work_buddy_logger.setLevel(previous_level)


def _worker_args(worker: str) -> list[str]:
    shared = ["--session-id", _SESSION_ID]
    if worker == "claude_worker":
        return ["--model", "sonnet", *shared, "--max-budget-usd", "1"]
    return [
        "--model",
        "fixture-model",
        *shared,
        "--mcp-url",
        "http://localhost:1/mcp",
    ]


def _module_entry(
    worker: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdin: object,
    args: list[str] | None = None,
) -> None:
    module_name = f"work_buddy.agent_execution.{worker}"
    # Other execution tests import these modules normally. Re-execute the entry
    # point with the same __name__ used by the real ``python -m`` process.
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    entry_args = _worker_args(worker) if args is None else args
    monkeypatch.setattr(sys, "argv", [module_name, *entry_args])
    monkeypatch.setattr(sys, "stdin", stdin)
    runpy.run_module(module_name, run_name="__main__", alter_sys=True)


def _read_worker_log(path: Path, worker: str) -> str:
    for handler in logging.getLogger("work_buddy").handlers:
        handler.flush()
    content = path.read_text(encoding="utf-8")
    assert f"work_buddy.agent_execution.{worker}" in content
    assert " | __main__ | " not in content
    assert _PRIVATE_BRIEF not in content
    assert _PRIVATE_ERROR not in content
    assert "Traceback" not in content
    return content


@pytest.mark.parametrize("worker", _WORKERS)
def test_module_entry_logs_request_rejection_in_session_file(
    monkeypatch: pytest.MonkeyPatch,
    worker_log: Path,
    worker: str,
) -> None:
    with pytest.raises(SystemExit) as stopped:
        _module_entry(worker, monkeypatch, stdin=io.StringIO(""))

    assert stopped.value.code == 2
    content = _read_worker_log(worker_log, worker)
    assert "stage=validate_request exit_code=2" in content


@pytest.mark.parametrize("worker", _WORKERS)
def test_module_entry_logs_argument_failure_without_argument_text(
    monkeypatch: pytest.MonkeyPatch,
    worker_log: Path,
    worker: str,
) -> None:
    with pytest.raises(SystemExit) as stopped:
        _module_entry(
            worker,
            monkeypatch,
            stdin=io.StringIO(_PRIVATE_BRIEF),
            args=["--unknown", _PRIVATE_ERROR],
        )

    assert stopped.value.code == 2
    content = _read_worker_log(worker_log, worker)
    assert "stage=parse_arguments exit_code=2" in content


@pytest.mark.parametrize("worker", _WORKERS)
def test_module_entry_logs_stdin_failure_without_exception_text(
    monkeypatch: pytest.MonkeyPatch,
    worker_log: Path,
    worker: str,
) -> None:
    class FailedInput:
        def read(self, _size: int) -> str:
            raise UnicodeError(_PRIVATE_ERROR)

    with pytest.raises(UnicodeError):
        _module_entry(worker, monkeypatch, stdin=FailedInput())

    content = _read_worker_log(worker_log, worker)
    assert "stage=read_brief error_type=UnicodeError" in content


@pytest.mark.parametrize(
    ("runtime_exit", "runtime_stdout", "worker_exit"),
    [
        (0, _PRIVATE_BRIEF, 0),
        (17, _PRIVATE_BRIEF, 1),
        (
            1,
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": True,
                    "result": _AUTH_FAILURE,
                }
            ),
            3,
        ),
    ],
)
def test_claude_module_entry_logs_only_child_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    worker_log: Path,
    runtime_exit: int,
    runtime_stdout: str,
    worker_exit: int,
) -> None:
    from work_buddy.agent_execution import claude_code

    source_config = tmp_path / "fake-account"
    source_config.mkdir()
    (source_config / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": _PRIVATE_ERROR}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(source_config))
    monkeypatch.setattr(claude_code, "_work_buddy_mcp_config", lambda _session: "{}")
    calls: list[dict[str, object]] = []

    def runtime(_command: list[str], **kwargs: object) -> object:
        calls.append(kwargs)
        os.write(kwargs["stdout"], runtime_stdout.encode("utf-8"))
        return SimpleNamespace(
            returncode=runtime_exit,
            stdout=None,
            stderr=_PRIVATE_ERROR,
        )

    monkeypatch.setattr(subprocess, "run", runtime)
    with io.TextIOWrapper(
        io.BytesIO(_PRIVATE_BRIEF.encode("utf-8")),
        encoding="cp1252",
    ) as stdin, pytest.raises(SystemExit) as stopped:
        _module_entry("claude_worker", monkeypatch, stdin=stdin)

    assert stopped.value.code == worker_exit
    assert len(calls) == 1
    assert calls[0]["input"] == _PRIVATE_BRIEF
    assert isinstance(calls[0]["stdout"], int)
    assert calls[0]["stdout"] >= 0
    assert calls[0]["stderr"] is subprocess.DEVNULL
    assert calls[0]["encoding"] == "utf-8"
    assert calls[0]["errors"] == "strict"
    content = _read_worker_log(worker_log, "claude_worker")
    assert "stage=run_runtime" in content
    if runtime_exit:
        assert f"stage=runtime_exit exit_code={runtime_exit}" in content
        assert f"worker_exit_code={worker_exit}" in content
    else:
        assert "stage=completed exit_code=0" in content
    assert str(source_config) not in content
    assert _AUTH_FAILURE not in content


def test_claude_module_entry_logs_command_construction_exception_safely(
    monkeypatch: pytest.MonkeyPatch,
    worker_log: Path,
) -> None:
    from work_buddy.agent_execution import claude_code

    def failed_config(_session: str) -> str:
        raise ValueError(_PRIVATE_ERROR)

    monkeypatch.setattr(claude_code, "_work_buddy_mcp_config", failed_config)
    with pytest.raises(ValueError):
        _module_entry(
            "claude_worker",
            monkeypatch,
            stdin=io.StringIO(_PRIVATE_BRIEF),
        )

    assert "stage=build_command error_type=ValueError" in _read_worker_log(
        worker_log, "claude_worker"
    )


def test_codex_module_entry_logs_sdk_startup_exception_safely(
    monkeypatch: pytest.MonkeyPatch,
    worker_log: Path,
) -> None:
    from work_buddy.agent_execution import codex

    def failed_sdk(**_kwargs: object) -> object:
        raise RuntimeError(_PRIVATE_ERROR)

    fake_sdk = ModuleType("openai_codex")
    fake_sdk.Codex = failed_sdk
    fake_sdk.ApprovalMode = SimpleNamespace(deny_all="deny-all")
    fake_sdk.Sandbox = SimpleNamespace(read_only="read-only")
    monkeypatch.setitem(sys.modules, "openai_codex", fake_sdk)
    monkeypatch.setattr(codex, "codex_subscription_config", lambda **_kwargs: {})
    with pytest.raises(SystemExit) as stopped:
        _module_entry(
            "codex_worker",
            monkeypatch,
            stdin=io.StringIO(_PRIVATE_BRIEF),
        )

    assert stopped.value.code == 1
    assert "stage=discover_config error_type=RuntimeError" in _read_worker_log(
        worker_log, "codex_worker"
    )


@pytest.mark.parametrize("extra_bytes", [0, 1, 128 * 1024])
def test_claude_stdout_pipe_drains_and_rejects_output_above_memory_cap(
    worker_log: Path,
    extra_bytes: int,
) -> None:
    from work_buddy.agent_execution.claude_worker import _run_with_bounded_stdout
    from work_buddy.agent_execution.worker_outcome import MAX_WORKER_RESULT_BYTES

    payload = b"x" * (MAX_WORKER_RESULT_BYTES + extra_bytes)

    def runtime(_command: list[str], *, stdout: int) -> object:
        with os.fdopen(os.dup(stdout), "wb") as output:
            output.write(payload)
        return SimpleNamespace(returncode=1)

    result, captured = _run_with_bounded_stdout(runtime, ["fixture-cli"])

    assert result.returncode == 1
    assert captured == (payload if extra_bytes == 0 else None)


def test_claude_stdout_pipe_closes_both_descriptors_after_child_start_failure(
    monkeypatch: pytest.MonkeyPatch,
    worker_log: Path,
) -> None:
    from work_buddy.agent_execution import claude_worker

    real_pipe = os.pipe
    descriptors: list[int] = []

    def observed_pipe() -> tuple[int, int]:
        pair = real_pipe()
        descriptors.extend(pair)
        return pair

    def failed_runtime(_command: list[str], *, stdout: int) -> object:
        raise FileNotFoundError(_PRIVATE_ERROR)

    monkeypatch.setattr(claude_worker.os, "pipe", observed_pipe)
    with pytest.raises(FileNotFoundError):
        claude_worker._run_with_bounded_stdout(failed_runtime, ["fixture-cli"])

    assert len(descriptors) == 2
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_claude_stdout_pipe_read_failure_is_not_classified(
    monkeypatch: pytest.MonkeyPatch,
    worker_log: Path,
) -> None:
    from work_buddy.agent_execution import claude_worker

    def failed_read(_descriptor: int, _size: int) -> bytes:
        raise OSError(_PRIVATE_ERROR)

    monkeypatch.setattr(claude_worker.os, "read", failed_read)
    result, captured = claude_worker._run_with_bounded_stdout(
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1),
        ["fixture-cli"],
    )

    assert result.returncode == 1
    assert captured is None


def test_claude_stdout_pipe_does_not_wait_for_descendant_held_handle(
    monkeypatch: pytest.MonkeyPatch,
    worker_log: Path,
) -> None:
    from work_buddy.agent_execution import claude_worker

    monkeypatch.setattr(claude_worker, "_STDOUT_JOIN_TIMEOUT_SECONDS", 0.01)
    held_descriptors: list[int] = []
    output: list[tuple[object, bytes | None]] = []
    ready = threading.Event()
    finished = threading.Event()
    read_finished = threading.Event()
    readers: list[threading.Thread] = []
    real_read = os.read

    def observed_read(descriptor: int, size: int) -> bytes:
        if not readers:
            readers.append(threading.current_thread())
        chunk = real_read(descriptor, size)
        if not chunk:
            read_finished.set()
        return chunk

    def runtime(_command: list[str], *, stdout: int) -> object:
        held_descriptors.append(os.dup(stdout))
        os.write(stdout, b'{"type":"result"}')
        ready.set()
        return SimpleNamespace(returncode=1)

    def call_worker() -> None:
        try:
            output.append(
                claude_worker._run_with_bounded_stdout(runtime, ["fixture-cli"])
            )
        finally:
            finished.set()

    monkeypatch.setattr(claude_worker.os, "read", observed_read)
    caller = threading.Thread(target=call_worker, daemon=True)
    caller.start()
    try:
        assert ready.wait(timeout=1)
        assert finished.wait(timeout=1), (
            "CLI completion must not wait for descendant stdout"
        )
        assert output[0][1] is None
        assert not read_finished.is_set()
    finally:
        for descriptor in held_descriptors:
            os.close(descriptor)
        caller.join(timeout=1)
        assert read_finished.wait(timeout=1)
        for reader in readers:
            reader.join(timeout=1)
            assert not reader.is_alive()
