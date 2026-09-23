"""establish_host_context: the decision table, on a simulated Windows.

A background host with no console gets one without a window, in-process
where Windows provides the API, otherwise by relaunching itself under the
console interpreter. Every path ends with the role's own identity.
"""

from __future__ import annotations

import logging
import os

import pytest

from work_buddy.process import HostRole, RecordingRunner, host, policy, tree
from work_buddy.process.policy import CREATE_NO_WINDOW, Intent


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """Restore the identity variables and the recorded context after each test."""
    monkeypatch.setenv("WORK_BUDDY_SESSION_ID", "agent-session")
    monkeypatch.setenv("CODEX_THREAD_ID", "codex-thread")
    monkeypatch.delenv(host.RELAUNCH_MARKER, raising=False)
    monkeypatch.delenv(host.FORCE_RELAUNCH, raising=False)
    monkeypatch.setattr(host, "_established", None)


@pytest.fixture
def windows(monkeypatch, tmp_path):
    """Simulate Windows, with a venv whose interpreters exist on disk."""
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    for name in ("python.exe", "pythonw.exe"):
        (scripts / name).write_text("")
    monkeypatch.setattr(policy, "is_windows", lambda platform=None: True)
    monkeypatch.setattr(host.sys, "executable", str(scripts / "pythonw.exe"))
    monkeypatch.setattr(host.sys, "argv", ["-m"])
    return scripts


def _console(monkeypatch, *, present: bool, window: bool = False) -> None:
    monkeypatch.setattr(host, "_console_state", lambda: (present, window))


def _no_allocation(monkeypatch) -> None:
    monkeypatch.setattr(host, "_allocate_windowless_console", lambda: False)


def test_an_inherited_hidden_console_is_kept(monkeypatch, windows) -> None:
    _console(monkeypatch, present=True, window=False)
    monkeypatch.setattr(
        host, "_allocate_windowless_console", lambda: pytest.fail("allocated a second console"),
    )

    context = host.establish_host_context(HostRole.SIDECAR)

    assert (context.console, context.mechanism) == ("hidden", "inherited")


def test_an_inherited_visible_console_is_reported_attached(monkeypatch, windows) -> None:
    _console(monkeypatch, present=True, window=True)

    context = host.establish_host_context(HostRole.SIDECAR)

    assert (context.console, context.mechanism) == ("attached", "inherited")


def test_no_console_allocates_one_without_a_window(monkeypatch, windows) -> None:
    _console(monkeypatch, present=False)
    monkeypatch.setattr(host, "_allocate_windowless_console", lambda: True)

    context = host.establish_host_context(HostRole.SIDECAR)

    assert (context.console, context.mechanism) == ("allocated", "allocate_api")
    assert context.image == "gui"


def test_without_the_api_the_host_relaunches_waits_and_exits_with_its_status(
    monkeypatch, windows,
) -> None:
    _console(monkeypatch, present=False)
    _no_allocation(monkeypatch)
    jobs: list[tuple[int, int]] = []
    monkeypatch.setattr(tree, "create_kill_on_close_job", lambda silent_breakaway=False: 77)
    monkeypatch.setattr(
        tree, "assign_process_to_job", lambda job, pid: jobs.append((job, pid)) or True,
    )
    runner = RecordingRunner().script(returncode=3)

    with pytest.raises(SystemExit) as exited:
        host.establish_host_context(HostRole.SIDECAR, module="work_buddy.sidecar", runner=runner)

    assert exited.value.code == 3
    [launch] = runner.launches
    assert launch.intent is Intent.HOST
    assert launch.argv[0] == str(windows / "python.exe")
    assert list(launch.argv[1:3]) == ["-m", "work_buddy.sidecar"]
    assert launch.creationflags == CREATE_NO_WINDOW
    assert launch.env[host.RELAUNCH_MARKER] == "1"
    # The relaunched copy is held in the parent's kill-on-close job, so ending
    # the parent (for example by ending the scheduled task) ends the daemon.
    assert jobs == [(77, runner.processes[0].pid)]


def test_the_job_asks_for_silent_breakaway(monkeypatch, windows) -> None:
    _console(monkeypatch, present=False)
    _no_allocation(monkeypatch)
    requested: list[bool] = []
    monkeypatch.setattr(
        tree, "create_kill_on_close_job",
        lambda silent_breakaway=False: requested.append(silent_breakaway) or 1,
    )
    monkeypatch.setattr(tree, "assign_process_to_job", lambda job, pid: True)

    with pytest.raises(SystemExit):
        host.establish_host_context(
            HostRole.SIDECAR, module="work_buddy.sidecar", runner=RecordingRunner(),
        )

    assert requested == [True]


def test_a_failed_job_assignment_is_logged_and_the_parent_still_waits(
    monkeypatch, windows, caplog,
) -> None:
    _console(monkeypatch, present=False)
    _no_allocation(monkeypatch)
    monkeypatch.setattr(tree, "create_kill_on_close_job", lambda silent_breakaway=False: 1)
    monkeypatch.setattr(tree, "assign_process_to_job", lambda job, pid: False)
    runner = RecordingRunner().script(returncode=0)

    with caplog.at_level(logging.WARNING, logger="work_buddy.process.host"):
        with pytest.raises(SystemExit) as exited:
            host.establish_host_context(
                HostRole.SIDECAR, module="work_buddy.sidecar", runner=runner,
            )

    assert exited.value.code == 0
    assert "not tied to this process" in caplog.text


@pytest.mark.parametrize(
    ("status", "code"),
    [(0, 0), (1, 1), (2**31 - 1, 2**31 - 1), (2**31, -(2**31)), (0xC0000005, -1073741819)],
)
def test_exit_status_maps_into_the_signed_range(status: int, code: int) -> None:
    assert host.signed_exit_status(status) == code
    assert host.signed_exit_status(status) & 0xFFFFFFFF == status


def test_the_relaunched_copy_never_relaunches_again(monkeypatch, windows) -> None:
    _console(monkeypatch, present=True, window=False)
    monkeypatch.setenv(host.RELAUNCH_MARKER, "1")

    context = host.establish_host_context(HostRole.SIDECAR, runner=RecordingRunner(strict=True))

    assert (context.console, context.mechanism) == ("hidden", "relaunch")


def test_forced_relaunch_skips_the_api(monkeypatch, windows) -> None:
    _console(monkeypatch, present=False)
    monkeypatch.setenv(host.FORCE_RELAUNCH, "1")
    monkeypatch.setattr(
        host, "_allocate_windowless_console", lambda: pytest.fail("used the API"),
    )
    monkeypatch.setattr(tree, "create_kill_on_close_job", lambda silent_breakaway=False: None)
    runner = RecordingRunner()

    with pytest.raises(SystemExit):
        host.establish_host_context(HostRole.SIDECAR, module="work_buddy.sidecar", runner=runner)

    assert len(runner.launches) == 1


def test_a_missing_console_interpreter_means_running_without_a_console(
    monkeypatch, windows, caplog,
) -> None:
    (windows / "python.exe").unlink()
    _console(monkeypatch, present=False)
    _no_allocation(monkeypatch)
    runner = RecordingRunner(strict=True)

    with caplog.at_level(logging.ERROR, logger="work_buddy.process.host"):
        context = host.establish_host_context(
            HostRole.SIDECAR, module="work_buddy.sidecar", runner=runner,
        )

    assert (context.console, context.mechanism) == ("none", "none")
    assert runner.launches == []
    assert "Running without a console" in caplog.text


def test_a_relaunch_that_cannot_start_means_running_without_a_console(
    monkeypatch, windows,
) -> None:
    _console(monkeypatch, present=False)
    _no_allocation(monkeypatch)
    runner = RecordingRunner().script(raises=OSError("access denied"))

    context = host.establish_host_context(HostRole.SIDECAR, module="work_buddy.sidecar", runner=runner)

    assert (context.console, context.mechanism) == ("none", "none")


def test_foreground_keeps_the_terminal_and_takes_a_sidecar_identity(monkeypatch, windows) -> None:
    monkeypatch.setenv("WORK_BUDDY_SESSION_ID", "wbuddy-cli")
    _console(monkeypatch, present=True, window=True)

    context = host.establish_host_context(HostRole.SIDECAR, foreground=True)

    assert (context.console, context.mechanism) == ("attached", "foreground")
    assert os.environ["WORK_BUDDY_SESSION_ID"].startswith("sidecar-")
    assert "CODEX_THREAD_ID" not in os.environ


def test_every_sidecar_start_takes_a_fresh_identity(monkeypatch, windows) -> None:
    _console(monkeypatch, present=True)

    host.establish_host_context(HostRole.SIDECAR)
    first = os.environ["WORK_BUDDY_SESSION_ID"]
    host.establish_host_context(HostRole.SIDECAR)

    assert first.startswith("sidecar-") and first != "agent-session"
    assert os.environ["WORK_BUDDY_SESSION_ID"] != first


def test_the_tray_needs_no_console_and_takes_the_cli_identity(monkeypatch, windows) -> None:
    monkeypatch.setattr(host, "_console_state", lambda: pytest.fail("probed the console"))

    context = host.establish_host_context(HostRole.TRAY)

    assert (context.console, context.mechanism) == ("not_applicable", "not_applicable")
    assert os.environ["WORK_BUDDY_SESSION_ID"] == host.CLI_SESSION_ID


def test_posix_has_no_console_to_establish(monkeypatch) -> None:
    monkeypatch.setattr(policy, "is_windows", lambda platform=None: False)

    context = host.establish_host_context(HostRole.SIDECAR)

    assert (context.console, context.mechanism) == ("not_applicable", "not_applicable")
    assert os.environ["WORK_BUDDY_SESSION_ID"].startswith("sidecar-")


# -- describe_host_context -------------------------------------------------------


def test_description_adds_the_child_interpreter_and_pin(monkeypatch, windows, tmp_path) -> None:
    _console(monkeypatch, present=True)
    host.establish_host_context(HostRole.SIDECAR)
    monkeypatch.setattr("work_buddy.paths.repo_root", lambda: tmp_path)
    pin = str(windows / "pythonw.exe")

    record = host.describe_host_context({"sidecar": {"python_executable": pin}})

    assert record["role"] == "sidecar"
    assert record["mechanism"] == "inherited"
    assert record["pin"] == pin
    # Services always run on the console image, even when the pin names
    # pythonw.exe.
    assert record["child_python"] == str(windows / "python.exe")
    assert record["child_image"] == "console"
    assert record["pin_outside_project"] is False


def test_description_flags_a_pin_outside_the_project(monkeypatch, windows, tmp_path) -> None:
    _console(monkeypatch, present=True)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr("work_buddy.paths.repo_root", lambda: project)

    record = host.describe_host_context(
        {"sidecar": {"python_executable": str(windows / "python.exe")}},
    )

    assert record["pin_outside_project"] is True


def test_description_without_a_pin(monkeypatch, windows) -> None:
    _console(monkeypatch, present=True)

    record = host.describe_host_context({})

    assert record["pin"] is None
    assert record["pin_outside_project"] is False
    assert record["role"] == "unknown"


# -- sibling images -------------------------------------------------------------------


def test_sibling_image_selects_the_role_image_in_the_same_directory(windows) -> None:
    console, matched = host.sibling_image(str(windows / "pythonw.exe"), "console")
    gui, gui_matched = host.sibling_image(str(windows / "python.exe"), "gui")

    assert (console, matched) == (str(windows / "python.exe"), True)
    assert (gui, gui_matched) == (str(windows / "pythonw.exe"), True)


def test_a_missing_sibling_is_reported_not_invented(windows) -> None:
    (windows / "pythonw.exe").unlink()

    path, matched = host.sibling_image(str(windows / "python.exe"), "gui")

    assert (path, matched) == (str(windows / "python.exe"), False)


def test_posix_has_one_image() -> None:
    assert host.sibling_image("/usr/bin/python3", "gui", platform="linux") == (
        "/usr/bin/python3", True,
    )
