"""Tests for the login auto-start backends.

Every OS call (PowerShell / systemctl / launchctl) and every filesystem write is
mocked or redirected to a temp dir, so the tests never register a real task,
unit, or agent on the host.
"""

from __future__ import annotations

import plistlib
import types

from work_buddy import autostart
from work_buddy.autostart import linux, macos, windows


def test_names_are_stable():
    # Kept in sync with notifications.service_hints and health checks.
    assert autostart.TASK_NAME == "WB-Sidecar"
    assert autostart.UNIT_NAME == "wb-sidecar"
    assert autostart.AGENT_LABEL == "com.workbuddy.sidecar"


def test_status_shape(monkeypatch):
    monkeypatch.setattr(autostart, "_backend", lambda: types.SimpleNamespace(is_registered=lambda: True))
    st = autostart.status()
    assert st["registered"] is True
    assert st["os"] in {"windows", "linux", "macos"}


# --- Windows (Task Scheduler) ---------------------------------------------

def _fake_cp(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_windows_register_builds_task(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(windows, "_run_ps", lambda script, timeout=60: calls.append(script) or _fake_cp())
    res = windows.register(
        python_exe=str(tmp_path / "python.exe"), home_dir=str(tmp_path), data_dir=str(tmp_path)
    )
    assert res["ok"] is True
    assert "Register-ScheduledTask" in calls[0]
    assert "WB-Sidecar" in calls[0]
    assert "-m work_buddy.sidecar" in calls[0]
    assert "-RunLevel Limited" in calls[0]  # per-user, no admin


def test_windows_is_registered(monkeypatch):
    monkeypatch.setattr(windows, "_run_ps", lambda script, timeout=30: _fake_cp(stdout="yes\n"))
    assert windows.is_registered() is True
    monkeypatch.setattr(windows, "_run_ps", lambda script, timeout=30: _fake_cp(stdout="no\n"))
    assert windows.is_registered() is False


def test_windows_register_reports_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(windows, "_run_ps", lambda script, timeout=60: _fake_cp(returncode=1, stderr="denied"))
    res = windows.register(python_exe="py.exe", home_dir=str(tmp_path), data_dir=str(tmp_path))
    assert res["ok"] is False and "denied" in res["detail"]


# --- Linux (systemd --user) -----------------------------------------------

def test_linux_register_writes_unit(monkeypatch, tmp_path):
    unit = tmp_path / "wb-sidecar.service"
    monkeypatch.setattr(linux, "_unit_path", lambda: unit)
    monkeypatch.setattr(linux, "_systemctl", lambda *a, **k: _fake_cp())
    res = linux.register(python_exe="/venv/bin/python", home_dir="/home/x", data_dir="/data")
    assert res["ok"] is True
    txt = unit.read_text()
    assert "ExecStart=/venv/bin/python -m work_buddy.sidecar" in txt
    assert "WORK_BUDDY_DATA_DIR=/data" in txt
    assert "WorkingDirectory=/home/x" in txt


def test_linux_is_registered(monkeypatch, tmp_path):
    unit = tmp_path / "wb-sidecar.service"
    monkeypatch.setattr(linux, "_unit_path", lambda: unit)
    assert linux.is_registered() is False  # no unit file yet
    unit.write_text("[Unit]\n")
    monkeypatch.setattr(linux, "_systemctl", lambda *a, **k: _fake_cp(stdout="enabled\n"))
    assert linux.is_registered() is True


# --- macOS (launchd) ------------------------------------------------------

def test_macos_register_writes_plist(monkeypatch, tmp_path):
    plist = tmp_path / "com.workbuddy.sidecar.plist"
    monkeypatch.setattr(macos, "_plist_path", lambda: plist)
    monkeypatch.setattr(macos, "_log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(macos.os, "getuid", lambda: 501, raising=False)
    monkeypatch.setattr(macos.subprocess, "run", lambda *a, **k: _fake_cp())
    res = macos.register(python_exe="/venv/bin/python", home_dir="/home/x", data_dir="/data")
    assert res["ok"] is True
    with open(plist, "rb") as fh:
        pl = plistlib.load(fh)
    assert pl["Label"] == "com.workbuddy.sidecar"
    assert pl["ProgramArguments"] == ["/venv/bin/python", "-m", "work_buddy.sidecar"]
    assert pl["EnvironmentVariables"]["WORK_BUDDY_DATA_DIR"] == "/data"
    assert pl["ProcessType"] == "Background"


def test_macos_is_registered(monkeypatch, tmp_path):
    plist = tmp_path / "com.workbuddy.sidecar.plist"
    monkeypatch.setattr(macos, "_plist_path", lambda: plist)
    assert macos.is_registered() is False
    plist.write_text("<plist/>")
    assert macos.is_registered() is True
