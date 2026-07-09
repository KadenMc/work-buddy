"""tray.ensure_running / stop_running: the Qt-free management surface.

The hard requirement under test: ``ensure_running`` NEVER raises and never
spawns when disabled / already running / missing the extra, because it runs
best-effort inside every ``wbuddy start``.
"""

from __future__ import annotations

import subprocess

import pytest

from work_buddy import tray


@pytest.fixture
def no_spawn(monkeypatch):
    calls: list[dict] = []

    def fake_popen(cmd, **kw):
        calls.append({"cmd": cmd, **kw})
        class _P:  # noqa: N801 - minimal Popen stand-in
            pid = 99999
        return _P()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    return calls


class TestEnsureRunning:
    def test_disabled_is_quiet_noop(self, monkeypatch, no_spawn):
        monkeypatch.setattr(tray, "is_enabled", lambda: False)
        res = tray.ensure_running()
        assert res["ok"] and not res["spawned"]
        assert "disabled" in res["detail"]
        assert no_spawn == []

    def test_already_running_noop(self, monkeypatch, no_spawn):
        monkeypatch.setattr(tray, "is_enabled", lambda: True)
        monkeypatch.setattr(tray, "running_pid", lambda: 4242)
        res = tray.ensure_running()
        assert res["ok"] and not res["spawned"] and res["pid"] == 4242
        assert no_spawn == []

    def test_missing_extra_reports_not_raises(self, monkeypatch, no_spawn):
        monkeypatch.setattr(tray, "is_enabled", lambda: True)
        monkeypatch.setattr(tray, "running_pid", lambda: None)
        monkeypatch.setattr(tray, "qt_available", lambda: False)
        res = tray.ensure_running()
        assert not res["ok"] and not res["spawned"]
        assert "--extra tray" in res["detail"]
        assert no_spawn == []

    def test_spawns_detached_module_without_session_id(self, monkeypatch, no_spawn):
        monkeypatch.setattr(tray, "is_enabled", lambda: True)
        monkeypatch.setattr(tray, "running_pid", lambda: None)
        monkeypatch.setattr(tray, "qt_available", lambda: True)
        monkeypatch.setenv("WORK_BUDDY_SESSION_ID", "should-not-inherit")
        res = tray.ensure_running()
        assert res["ok"] and res["spawned"]
        assert len(no_spawn) == 1
        call = no_spawn[0]
        assert call["cmd"][-2:] == ["-m", "work_buddy.tray"]
        assert "WORK_BUDDY_SESSION_ID" not in call["env"]
        assert call["stdout"] is subprocess.DEVNULL

    def test_never_raises(self, monkeypatch):
        monkeypatch.setattr(tray, "is_enabled", lambda: True)
        monkeypatch.setattr(tray, "running_pid", lambda: None)
        monkeypatch.setattr(tray, "qt_available", lambda: True)

        def boom(*a, **k):
            raise OSError("no such interpreter")

        monkeypatch.setattr(subprocess, "Popen", boom)
        res = tray.ensure_running()
        assert not res["ok"]
        assert "failed" in res["detail"]


class TestStopRunning:
    def test_not_running(self, monkeypatch):
        from work_buddy.tray import pidfile

        monkeypatch.setattr(pidfile, "check_existing_tray", lambda: None)
        res = tray.stop_running()
        assert res["ok"] and not res["stopped"]

    def test_graceful_stop_withdraws_pidfile(self, monkeypatch):
        from work_buddy.tray import pidfile

        withdrew = []
        monkeypatch.setattr(pidfile, "check_existing_tray", lambda: 4242)
        monkeypatch.setattr(pidfile, "withdraw", lambda: withdrew.append(True))
        # Process "exits" as soon as the signal lands: alive returns False.
        import work_buddy.utils.process as proc

        monkeypatch.setattr(proc, "is_process_alive", lambda pid: False)
        res = tray.stop_running(wait_seconds=0.5)
        assert withdrew == [True]
        assert res["ok"] and res["stopped"] and res["pid"] == 4242
