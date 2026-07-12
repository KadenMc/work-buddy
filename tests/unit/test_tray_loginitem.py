"""Login-item parameterization: WB-Tray artifacts, sidecar defaults unchanged.

The autostart backends grew (name, module, description) parameters so the
tray reuses the exact per-OS template. These tests pin BOTH directions: the
tray call produces WB-Tray / -m work_buddy.tray artifacts, and the default
(sidecar) call still produces byte-identical WB-Sidecar behavior.
"""

from __future__ import annotations

import plistlib
import subprocess

import pytest

from work_buddy import autostart
from work_buddy.autostart import linux as linux_backend
from work_buddy.autostart import macos as macos_backend
from work_buddy.autostart import windows as windows_backend


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


class TestWindowsBackend:
    @pytest.fixture
    def scripts(self, monkeypatch):
        seen: list[str] = []

        def fake_run_ps(script, timeout=60):
            seen.append(script)
            return _completed()

        monkeypatch.setattr(windows_backend, "_run_ps", fake_run_ps)
        return seen

    def test_tray_registration_script(self, scripts):
        res = windows_backend.register(
            python_exe=r"C:\x\.venv\Scripts\python.exe",
            home_dir=r"C:\x",
            data_dir=r"C:\data",
            name="WB-Tray",
            module="work_buddy.tray",
            description="work-buddy tray icon",
        )
        assert res["ok"]
        register_script = scripts[-1]
        assert "'WB-Tray'" in register_script
        assert "-m work_buddy.tray" in register_script
        assert "work-buddy tray icon" in register_script
        assert "WB-Sidecar" not in register_script

    def test_sidecar_default_unchanged(self, scripts):
        res = windows_backend.register(
            python_exe=r"C:\x\.venv\Scripts\python.exe",
            home_dir=r"C:\x",
            data_dir=r"C:\data",
        )
        assert res["ok"]
        register_script = scripts[-1]
        assert "'WB-Sidecar'" in register_script
        assert "-m work_buddy.sidecar" in register_script
        assert "work-buddy sidecar daemon" in register_script

    def test_unregister_targets_named_task(self, scripts):
        windows_backend.unregister(name="WB-Tray")
        assert "'WB-Tray'" in scripts[-1]


class TestLinuxBackend:
    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            linux_backend, "_unit_path",
            lambda unit=None: tmp_path / f"{unit or linux_backend.UNIT_NAME}.service",
        )
        monkeypatch.setattr(
            linux_backend, "_systemctl",
            lambda *a, **k: _completed(stdout="enabled"),
        )
        return tmp_path

    def test_tray_unit_content(self, env):
        res = linux_backend.register(
            python_exe="/x/.venv/bin/python",
            home_dir="/x",
            data_dir="/data",
            name="wb-tray",
            module="work_buddy.tray",
            description="work-buddy tray icon",
        )
        assert res["ok"]
        text = (env / "wb-tray.service").read_text()
        assert 'ExecStart="/x/.venv/bin/python" -m work_buddy.tray' in text
        assert "Description=work-buddy tray icon" in text

    def test_sidecar_default_unchanged(self, env):
        linux_backend.register(
            python_exe="/x/.venv/bin/python", home_dir="/x", data_dir="/data",
        )
        text = (env / "wb-sidecar.service").read_text()
        assert 'ExecStart="/x/.venv/bin/python" -m work_buddy.sidecar' in text
        assert "Description=work-buddy sidecar daemon" in text


class TestMacosPlist:
    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            macos_backend, "_plist_path",
            lambda label=None: tmp_path / f"{label or macos_backend.AGENT_LABEL}.plist",
        )
        monkeypatch.setattr(macos_backend, "_log_dir", lambda: tmp_path / "logs")
        return tmp_path

    def test_tray_plist_run_at_load_only(self, env):
        path = macos_backend._write_plist(
            "/x/python", "/x", "/data",
            label="com.workbuddy.tray", module="work_buddy.tray",
            keep_alive=False, log_basename="tray",
        )
        plist = plistlib.loads(path.read_bytes())
        assert plist["Label"] == "com.workbuddy.tray"
        assert plist["ProgramArguments"][-2:] == ["-m", "work_buddy.tray"]
        assert "KeepAlive" not in plist  # tray is RunAtLoad-only
        assert plist["StandardOutPath"].endswith("tray.out.log")

    def test_sidecar_plist_keeps_keepalive(self, env):
        path = macos_backend._write_plist(
            "/x/python", "/x", "/data",
            label=macos_backend.AGENT_LABEL, module="work_buddy.sidecar",
            keep_alive=True, log_basename="sidecar",
        )
        plist = plistlib.loads(path.read_bytes())
        assert plist["KeepAlive"] == {"SuccessfulExit": False}
        assert plist["ProgramArguments"][-2:] == ["-m", "work_buddy.sidecar"]


class TestPackageSurface:
    def test_register_tray_parameterizes_backend(self, monkeypatch):
        seen: dict = {}

        class _Stub:
            @staticmethod
            def register(**kw):
                seen.update(kw)
                return {"ok": True, "detail": "stub"}

            @staticmethod
            def is_registered(**kw):
                seen.update(kw)
                return True

            @staticmethod
            def unregister(**kw):
                seen.update(kw)
                return {"ok": True, "detail": "stub"}

        monkeypatch.setattr(autostart, "_backend", lambda: _Stub)
        autostart.register_tray(python_exe="p", home_dir="h", data_dir="d")
        assert seen["module"] == "work_buddy.tray"
        assert seen["name"] in (
            autostart.TRAY_TASK_NAME,
            autostart.TRAY_UNIT_NAME,
            autostart.TRAY_AGENT_LABEL,
        )
        assert seen["name"] != autostart.TASK_NAME

        seen.clear()
        assert autostart.tray_is_registered() is True
        assert seen["name"] != autostart.TASK_NAME
