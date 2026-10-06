"""Every way work-buddy starts one of its own hosts yields the role's context.

Recorded on a simulated Windows, where the difference matters: a background
host must run on the console image with a console of its own and no window,
and the tray on the GUI image, however the launching process was started.
"""

from __future__ import annotations

import subprocess

import pytest

from work_buddy.process import host, policy
from work_buddy.process.policy import CREATE_NO_WINDOW, Intent


@pytest.fixture
def venv(monkeypatch, tmp_path):
    """Simulate Windows, launched from a GUI (``pythonw.exe``) process."""
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    for name in ("python.exe", "pythonw.exe"):
        (scripts / name).write_text("")
    monkeypatch.setattr(policy, "is_windows", lambda platform=None: True)
    monkeypatch.setattr(host.sys, "executable", str(scripts / "pythonw.exe"))
    return scripts


def test_start_sidecar_from_a_gui_process_starts_the_console_image(
    monkeypatch, venv, recording_runner,
) -> None:
    from work_buddy.cli import lifecycle

    monkeypatch.setattr(lifecycle._pid, "check_existing_daemon", lambda: None)
    monkeypatch.setattr(lifecycle._state, "load_state", lambda: None)
    monkeypatch.setattr(lifecycle.time, "sleep", lambda _s: None)
    monkeypatch.setenv("WORK_BUDDY_SESSION_ID", "wbuddy-cli")

    lifecycle.start_sidecar(wait_seconds=0.1)

    [launch] = recording_runner.launches
    assert launch.intent is Intent.HOST
    assert launch.argv == (str(venv / "python.exe"), "-m", "work_buddy.sidecar")
    assert launch.creationflags == CREATE_NO_WINDOW
    assert launch.stdin == launch.stdout == launch.stderr == subprocess.DEVNULL
    assert "WORK_BUDDY_SESSION_ID" not in launch.env


def test_the_tray_starts_on_the_gui_image(monkeypatch, venv, recording_runner) -> None:
    from work_buddy import tray

    monkeypatch.setattr(tray, "is_enabled", lambda: True)
    monkeypatch.setattr(tray, "running_pid", lambda: None)
    monkeypatch.setattr(tray, "qt_available", lambda: True)
    monkeypatch.setattr(host, "resolve_child_python", lambda cfg=None: str(venv / "python.exe"))

    assert tray.ensure_running()["spawned"] is True

    [launch] = recording_runner.launches
    assert launch.argv == (str(venv / "pythonw.exe"), "-m", "work_buddy.tray")
    assert launch.env["WORK_BUDDY_SESSION_ID"] == host.CLI_SESSION_ID
    assert launch.env["PYTHONUTF8"] == "1"


def test_services_start_on_the_console_image_even_when_the_pin_names_pythonw(
    monkeypatch, venv, recording_runner, tmp_path,
) -> None:
    from work_buddy.sidecar import daemon

    monkeypatch.setattr(host, "resolve_child_python", lambda cfg=None: str(venv / "pythonw.exe"))
    monkeypatch.setattr(daemon, "_kill_process_on_port", lambda port, service_name="": True)
    (tmp_path / "logs").mkdir()
    monkeypatch.setattr("work_buddy.paths.data_dir", lambda sub="": tmp_path / "logs")
    monkeypatch.setattr(daemon, "assign_process_to_job", lambda job, pid: True)
    svc = daemon.ChildService(
        name="embedding", module="work_buddy.embedding", port=5124,
        args=["--flag"], environment={"EXTRA": "1"},
    )

    daemon._start_child(svc)

    [launch] = recording_runner.launches
    assert launch.argv == (
        str(venv / "python.exe"), "-u", "-m", "work_buddy.embedding", "--flag",
    )
    assert launch.creationflags == CREATE_NO_WINDOW
    assert launch.stderr == subprocess.STDOUT
    assert launch.stdout.name == str(tmp_path / "logs" / "embedding.log")
    assert launch.env["EXTRA"] == "1"
    assert launch.env["PYTHONUTF8"] == "1"
    assert svc.process is recording_runner.processes[0]
    assert "--- embedding starting at" in (tmp_path / "logs" / "embedding.log").read_text()


def test_messaging_auto_start_matches_the_supervised_service(
    monkeypatch, venv, recording_runner, tmp_path,
) -> None:
    from work_buddy.messaging import client

    monkeypatch.setattr(host, "resolve_child_python", lambda cfg=None: str(venv / "python.exe"))
    monkeypatch.setattr(client, "is_service_running", lambda: False)
    monkeypatch.setattr(client.time, "sleep", lambda _s: None)
    monkeypatch.setattr("work_buddy.paths.data_dir", lambda sub="": tmp_path)

    assert client._ensure_service_running() is False

    [launch] = recording_runner.launches
    assert launch.argv == (
        str(venv / "python.exe"), "-u", "-m", "work_buddy.messaging.service",
    )
    assert launch.creationflags == CREATE_NO_WINDOW
    assert launch.stdout.name == str(tmp_path / "messaging.log")


def test_messaging_auto_start_is_detached_on_posix(monkeypatch, recording_runner, tmp_path) -> None:
    from work_buddy.messaging import client

    monkeypatch.setattr(policy, "is_windows", lambda platform=None: False)
    monkeypatch.setattr(client, "is_service_running", lambda: False)
    monkeypatch.setattr(client.time, "sleep", lambda _s: None)
    monkeypatch.setattr("work_buddy.paths.data_dir", lambda sub="": tmp_path)

    client._ensure_service_running()

    [launch] = recording_runner.launches
    assert launch.start_new_session is True
    assert launch.creationflags == 0
