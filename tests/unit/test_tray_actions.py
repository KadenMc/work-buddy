"""tray.actions.open_dashboard: smart focus-or-create with a plain-open fallback."""

from __future__ import annotations

import pytest

from work_buddy.tray import actions


@pytest.fixture(autouse=True)
def _local_url(monkeypatch):
    import work_buddy.cli.commands as commands

    monkeypatch.setattr(commands, "dashboard_local_url", lambda: "http://127.0.0.1:5127")


class TestOpenDashboard:
    def test_uses_extension_when_it_responds(self, monkeypatch):
        seen = {}

        def fake_focus(url, target_hash="", timeout_seconds=15):
            seen["url"] = url
            seen["hash"] = target_hash
            return {"created": False, "focused": True}

        import work_buddy.collectors.chrome_collector as cc

        monkeypatch.setattr(cc, "focus_or_create_tab", fake_focus)
        # webbrowser must NOT be called on the happy path
        import webbrowser

        monkeypatch.setattr(webbrowser, "open", lambda *a, **k: pytest.fail("fallback used"))

        res = actions.open_dashboard(actions.ACTIVITY_HASH)
        assert res == {"ok": True, "via": "extension", "result": {"created": False, "focused": True}}
        assert seen["url"] == "http://127.0.0.1:5127"
        assert seen["hash"] == "#tab=settings&st=activity"

    def test_falls_back_when_extension_times_out(self, monkeypatch):
        import work_buddy.collectors.chrome_collector as cc

        monkeypatch.setattr(cc, "focus_or_create_tab", lambda *a, **k: None)
        opened = {}
        import webbrowser

        monkeypatch.setattr(webbrowser, "open", lambda url: opened.setdefault("url", url))

        res = actions.open_dashboard()
        assert res["via"] == "webbrowser"
        assert opened["url"] == "http://127.0.0.1:5127"

    def test_falls_back_when_extension_raises(self, monkeypatch):
        import work_buddy.collectors.chrome_collector as cc

        def boom(*a, **k):
            raise RuntimeError("no native host")

        monkeypatch.setattr(cc, "focus_or_create_tab", boom)
        opened = {}
        import webbrowser

        monkeypatch.setattr(webbrowser, "open", lambda url: opened.setdefault("url", url))

        res = actions.open_dashboard("#tab=settings&st=activity")
        assert res["via"] == "webbrowser"
        assert opened["url"] == "http://127.0.0.1:5127#tab=settings&st=activity"


class TestRestartAction:
    def test_restart_aborts_if_stop_fails(self, monkeypatch):
        from work_buddy.cli import lifecycle

        monkeypatch.setattr(
            lifecycle, "stop_sidecar",
            lambda: {"was_running": True, "stopped": False, "detail": "stuck"},
        )
        started = {"called": False}
        monkeypatch.setattr(
            lifecycle, "start_sidecar",
            lambda: started.update(called=True) or {"started": True},
        )
        res = actions.restart_sidecar()
        assert res["stopped"] is False
        assert started["called"] is False  # never tried to start after a failed stop
