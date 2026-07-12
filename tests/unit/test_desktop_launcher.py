from __future__ import annotations

from work_buddy import desktop_launcher


def test_desktop_launcher_success_is_silent_and_logged(tmp_path, monkeypatch):
    log_path = tmp_path / "desktop_launcher.log"
    monkeypatch.setattr(desktop_launcher, "launcher_log_path", lambda: log_path)
    monkeypatch.setattr(
        "work_buddy.cli.commands.launch_dashboard_app",
        lambda: {"ok": True, "url": "http://127.0.0.1:5127/app/"},
    )
    shown = []
    monkeypatch.setattr(desktop_launcher, "_show_native_error", lambda *args: shown.append(args))

    assert desktop_launcher.main() == 0
    assert shown == []
    assert "OK | Opened http://127.0.0.1:5127/app/" in log_path.read_text(encoding="utf-8")


def test_desktop_launcher_failure_shows_log_pointer(tmp_path, monkeypatch):
    log_path = tmp_path / "desktop_launcher.log"
    monkeypatch.setattr(desktop_launcher, "launcher_log_path", lambda: log_path)
    monkeypatch.setattr(
        "work_buddy.cli.commands.launch_dashboard_app",
        lambda: {"ok": False, "detail": "Dashboard app did not become ready."},
    )
    shown = []
    monkeypatch.setattr(desktop_launcher, "_show_native_error", lambda *args: shown.append(args))

    assert desktop_launcher.main() == 1
    assert shown == [("Dashboard app did not become ready.", log_path)]
    assert "ERROR | Dashboard app did not become ready." in log_path.read_text(encoding="utf-8")


def test_desktop_launcher_unexpected_error_is_logged_and_reported(tmp_path, monkeypatch):
    log_path = tmp_path / "desktop_launcher.log"
    monkeypatch.setattr(desktop_launcher, "launcher_log_path", lambda: log_path)

    def explode():
        raise RuntimeError("boom")

    monkeypatch.setattr("work_buddy.cli.commands.launch_dashboard_app", explode)
    shown = []
    monkeypatch.setattr(desktop_launcher, "_show_native_error", lambda *args: shown.append(args))

    assert desktop_launcher.main() == 1
    assert shown == [("Unexpected launcher error: boom", log_path)]
    logged = log_path.read_text(encoding="utf-8")
    assert "ERROR | Unexpected launcher error: boom" in logged
    assert "RuntimeError: boom" in logged
