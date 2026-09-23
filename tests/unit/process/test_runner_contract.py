"""One contract, two runners: the real one and the recording fake.

Every case runs through ``run_tool`` against both. The real runner launches
tiny ``sys.executable -c`` programs. The fake is scripted with what those
programs do, so a test written against the fake sees the same types, errors
and results that production code sees.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import time

import pytest

from work_buddy.process import (
    ExecutableNotFound,
    ProcessResult,
    ProcessTimeout,
    RecordingRunner,
    SubprocessRunner,
    run_tool,
    spawn_worker,
    use_runner,
)


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def _ended(pid: int) -> bool:
    """True once ``pid`` has exited. A killed process whose parent has not
    reaped it yet (a Linux zombie) has exited too."""
    from work_buddy.utils.process import is_process_alive

    if not is_process_alive(pid):
        return True
    try:
        stat = pathlib.Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[-1].split()[0] == "Z"


@pytest.fixture(params=["real", "fake"])
def runner(request):
    """Yields ``(kind, runner)``. Cases script the fake for its argv."""
    if request.param == "real":
        yield "real", SubprocessRunner()
    else:
        yield "fake", RecordingRunner(strict=True)


def test_exit_code_and_output_are_captured(runner) -> None:
    kind, active = runner
    argv = _py("import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)")
    if kind == "fake":
        active.script(argv, returncode=3, stdout="out\n", stderr="err\n")

    result = run_tool(argv, timeout=30, runner=active)

    assert isinstance(result, ProcessResult)
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == 3
    assert result.stdout.replace("\r\n", "\n") == "out\n"
    assert result.stderr.replace("\r\n", "\n") == "err\n"
    assert result.args == argv


def test_check_raises_called_process_error(runner) -> None:
    kind, active = runner
    argv = _py("import sys; sys.exit(2)")
    if kind == "fake":
        active.script(argv, returncode=2)

    with pytest.raises(subprocess.CalledProcessError) as caught:
        run_tool(argv, timeout=30, check=True, runner=active)

    assert caught.value.returncode == 2


def test_input_reaches_the_program(runner) -> None:
    kind, active = runner
    argv = _py("import sys; sys.stdout.write(sys.stdin.read().upper())")
    if kind == "fake":
        active.script(argv, respond=lambda launch: ProcessResult(
            list(launch.argv), 0, launch.input.upper(), "",
        ))

    result = run_tool(argv, timeout=30, input="hello", runner=active)

    assert result.stdout == "HELLO"


def test_bytes_mode_returns_bytes(runner) -> None:
    kind, active = runner
    argv = _py("import sys; sys.stdout.buffer.write(bytes([0, 255]))")
    if kind == "fake":
        active.script(argv, stdout=b"\x00\xff")

    result = run_tool(argv, timeout=30, text=False, runner=active)

    assert result.stdout == b"\x00\xff"
    assert isinstance(result.stderr, bytes)


def test_undecodable_output_is_replaced_when_asked(runner) -> None:
    kind, active = runner
    argv = _py("import sys; sys.stdout.buffer.write(bytes([255, 254]) + b'ok')")
    if kind == "fake":
        active.script(argv, stdout="\ufffd\ufffdok")

    result = run_tool(argv, timeout=30, encoding="utf-8", errors="replace", runner=active)

    assert result.stdout == "\ufffd\ufffdok"


def test_missing_program_raises_executable_not_found(runner) -> None:
    kind, active = runner
    argv = ["work-buddy-no-such-program-xyz"]
    if kind == "fake":
        active.script(argv, raises=ExecutableNotFound(2, "program not found", argv[0]))

    with pytest.raises(ExecutableNotFound) as caught:
        run_tool(argv, timeout=30, runner=active)

    # Existing handlers catch FileNotFoundError, so the subclass must stay one.
    assert isinstance(caught.value, FileNotFoundError)


def test_timeout_raises_process_timeout(runner) -> None:
    kind, active = runner
    argv = _py("import time; time.sleep(60)")
    if kind == "fake":
        active.script(argv, raises=ProcessTimeout(argv, 1.0))

    started = time.monotonic()
    with pytest.raises(ProcessTimeout) as caught:
        run_tool(argv, timeout=1.0, runner=active)

    # Existing handlers catch TimeoutExpired, and the resilience framework
    # classifies TimeoutError, so the error must be both.
    assert isinstance(caught.value, subprocess.TimeoutExpired)
    assert isinstance(caught.value, TimeoutError)
    assert caught.value.timeout == 1.0
    assert time.monotonic() - started < 30


def test_timeout_keeps_partial_output() -> None:
    argv = _py("import sys, time; print('started', flush=True); time.sleep(60)")

    with pytest.raises(ProcessTimeout) as caught:
        run_tool(argv, timeout=2.0, runner=SubprocessRunner())

    assert "started" in (caught.value.output or "")


def test_timeout_ends_the_whole_process_tree(tmp_path) -> None:
    """A grandchild must not outlive a timed-out tool run."""
    marker = tmp_path / "grandchild.pid"
    grandchild = "import time; time.sleep(60)"
    parent = (
        "import subprocess, sys, time\n"
        f"child = subprocess.Popen([sys.executable, '-c', {grandchild!r}])\n"
        f"open({str(marker)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(60)\n"
    )

    with pytest.raises(ProcessTimeout):
        run_tool(_py(parent), timeout=5.0, runner=SubprocessRunner())

    grandchild_pid = int(marker.read_text())
    deadline = time.monotonic() + 10
    while not _ended(grandchild_pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert _ended(grandchild_pid)


def test_passed_deadline_starts_nothing() -> None:
    from work_buddy.resilience import Deadline, ResilienceContext, use_context

    fake = RecordingRunner(strict=True)
    expired = ResilienceContext(operation_key="test", deadline=Deadline(at=0.0))
    with use_context(expired), pytest.raises(ProcessTimeout):
        run_tool(["git", "status"], timeout=5, runner=fake)

    assert fake.launches == []


# -- the override and the fake's own behaviour ---------------------------------------


def test_use_runner_captures_launches_and_restores_the_previous_runner() -> None:
    outer = RecordingRunner()
    inner = RecordingRunner()
    with use_runner(outer):
        run_tool(["git", "status"], timeout=5)
        with use_runner(inner):
            run_tool(["gh", "auth", "status"], timeout=5)
        run_tool(["git", "log"], timeout=5)

    assert outer.argvs() == [["git", "status"], ["git", "log"]]
    assert inner.argvs() == [["gh", "auth", "status"]]


def test_use_runner_captures_launches_from_other_threads() -> None:
    import threading

    fake = RecordingRunner()
    with use_runner(fake):
        worker = threading.Thread(target=lambda: run_tool(["git", "status"], timeout=5))
        worker.start()
        worker.join()

    assert fake.argvs() == [["git", "status"]]


def test_the_most_recent_matching_rule_answers() -> None:
    fake = RecordingRunner()
    fake.script(["git"], stdout="general")
    fake.script(["git", "show"], stdout="specific")

    assert run_tool(["git", "show", "abc"], timeout=5, runner=fake).stdout == "specific"
    assert run_tool(["git", "log"], timeout=5, runner=fake).stdout == "general"


def test_strict_fake_rejects_unscripted_launches() -> None:
    with pytest.raises(AssertionError):
        run_tool(["git", "status"], timeout=5, runner=RecordingRunner(strict=True))


def test_fake_worker_runs_until_ended() -> None:
    fake = RecordingRunner()
    fake.script(["node"], running=True, returncode=0)

    handle = spawn_worker(["node", "kernel.js"], stdin=subprocess.PIPE, runner=fake)

    assert handle.poll() is None
    with pytest.raises(subprocess.TimeoutExpired):
        handle.wait(timeout=0.01)
    handle.terminate()
    assert handle.poll() is not None
    assert fake.processes == [handle]


def test_real_worker_is_started_and_owned_by_the_caller() -> None:
    handle = spawn_worker(
        _py("import sys; sys.stdout.write(sys.stdin.read()[::-1])"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        runner=SubprocessRunner(),
    )

    out, _ = handle.communicate("abc", timeout=30)

    assert out == "cba"
    assert handle.returncode == 0
