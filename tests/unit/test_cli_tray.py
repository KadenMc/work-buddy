"""``wbuddy tray`` dispatch wiring + the status verb's JSON contract."""

from __future__ import annotations

import json

import pytest

from work_buddy.cli import commands, dispatch


class TestDispatchWiring:
    @pytest.fixture
    def recorded(self, monkeypatch):
        seen = {}

        def fake_cmd_tray(args):
            seen["tray_command"] = args.tray_command
            return 0

        monkeypatch.setattr(commands, "cmd_tray", fake_cmd_tray)
        return seen

    @pytest.mark.parametrize("verb", ["enable", "disable", "status", "run"])
    def test_verbs_route_to_cmd_tray(self, recorded, verb):
        assert dispatch.main(["tray", verb]) == 0
        assert recorded["tray_command"] == verb

    def test_missing_subcommand_is_usage_error(self):
        assert dispatch.main(["tray"]) == 2


class TestStatusVerb:
    def test_status_json_shape(self, monkeypatch, capsys):
        import work_buddy.config as config_mod
        from work_buddy import autostart, tray

        monkeypatch.setattr(
            config_mod, "load_config", lambda *a, **k: {"tray": {"enabled": True}}
        )
        monkeypatch.setattr(autostart, "tray_is_registered", lambda: False)
        monkeypatch.setattr(tray, "running_pid", lambda: None)

        rc = dispatch.main(["tray", "status", "--json"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out == {
            "enabled": True,
            "registered": False,
            "running": False,
            "pid": None,
        }

    def test_status_human_line(self, monkeypatch, capsys):
        import work_buddy.config as config_mod
        from work_buddy import autostart, tray

        monkeypatch.setattr(
            config_mod, "load_config", lambda *a, **k: {"tray": {"enabled": False}}
        )
        monkeypatch.setattr(autostart, "tray_is_registered", lambda: True)
        monkeypatch.setattr(tray, "running_pid", lambda: 4242)

        rc = dispatch.main(["tray", "status"])
        assert rc == 0
        line = capsys.readouterr().out
        assert "disabled" in line
        assert "registered" in line
        assert "running (pid=4242)" in line


class TestEnsureTrayHook:
    """The resurrection hook must never affect `wbuddy start`'s outcome."""

    def test_quiet_when_disabled(self, monkeypatch, capsys):
        from work_buddy import tray

        monkeypatch.setattr(
            tray, "ensure_running",
            lambda: {"ok": True, "spawned": False, "detail": "tray disabled"},
        )
        commands._ensure_tray()
        assert capsys.readouterr().out == ""

    def test_prints_when_spawned(self, monkeypatch, capsys):
        from work_buddy import tray

        monkeypatch.setattr(
            tray, "ensure_running",
            lambda: {"ok": True, "spawned": True, "detail": "tray spawned"},
        )
        commands._ensure_tray()
        assert "tray spawned" in capsys.readouterr().out

    def test_swallow_everything(self, monkeypatch):
        from work_buddy import tray

        def boom():
            raise RuntimeError("tray exploded")

        monkeypatch.setattr(tray, "ensure_running", boom)
        commands._ensure_tray()  # must not raise
