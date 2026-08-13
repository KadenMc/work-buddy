"""tray.actions.open_dashboard: smart focus-or-create with a plain-open fallback."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from work_buddy.tray import actions


@pytest.fixture(autouse=True)
def _local_url(monkeypatch):
    import work_buddy.cli.commands as commands

    monkeypatch.setattr(commands, "dashboard_local_url", lambda: "http://127.0.0.1:5127")


class TestOpenDashboard:
    def test_uses_extension_when_it_responds(self, monkeypatch):
        seen = {}

        def fake_focus(
            url, target_hash="", preserve_path=False, timeout_seconds=15
        ):
            seen["url"] = url
            seen["hash"] = target_hash
            seen["preserve_path"] = preserve_path
            return {
                "status": "ok",
                "details": {"created": False, "focused": True},
            }

        import work_buddy.collectors.chrome_collector as cc

        monkeypatch.setattr(cc, "focus_or_create_tab", fake_focus)
        # webbrowser must NOT be called on the happy path
        import webbrowser

        monkeypatch.setattr(webbrowser, "open", lambda *a, **k: pytest.fail("fallback used"))

        res = actions.open_dashboard(actions.ACTIVITY_HASH)
        assert res == {
            "ok": True,
            "via": "extension",
            "result": {
                "status": "ok",
                "details": {"created": False, "focused": True},
            },
        }
        assert seen["url"] == "http://127.0.0.1:5127"
        assert seen["hash"] == "#tab=settings&st=activity"
        assert seen["preserve_path"] is False

    def test_app_launch_preserves_the_existing_react_route(self, monkeypatch):
        seen = {}

        monkeypatch.setattr(
            "work_buddy.dashboard.local_identity_launch.bootstrap_fragment_for_dashboard",
            lambda app_url, *, next_hash="": "#wb-bootstrap=wbb_test",
        )

        def fake_focus(
            url, target_hash="", preserve_path=False, timeout_seconds=15
        ):
            seen.update(
                url=url,
                target_hash=target_hash,
                preserve_path=preserve_path,
                timeout_seconds=timeout_seconds,
            )
            return {
                "status": "ok",
                "details": {"created": False, "focused": True},
            }

        monkeypatch.setattr(
            "work_buddy.collectors.chrome_collector.focus_or_create_tab",
            fake_focus,
        )
        monkeypatch.setattr(
            "webbrowser.open", lambda *a, **k: pytest.fail("fallback used")
        )

        result = actions.open_dashboard(app=True)

        assert result["identity_bootstrap"] is True
        assert seen == {
            "url": "http://127.0.0.1:5127/app/",
            "target_hash": "#wb-bootstrap=wbb_test",
            "preserve_path": True,
            "timeout_seconds": 10,
        }

    def test_app_launch_fails_closed_when_bootstrap_cannot_be_minted(
        self,
        monkeypatch,
    ):
        monkeypatch.setattr(
            "work_buddy.dashboard.local_identity_launch.bootstrap_fragment_for_dashboard",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("broken")),
        )
        monkeypatch.setattr(
            "work_buddy.collectors.chrome_collector.focus_or_create_tab",
            lambda *args, **kwargs: pytest.fail("extension launch must not run"),
        )
        monkeypatch.setattr(
            "webbrowser.open",
            lambda *args, **kwargs: pytest.fail("browser launch must not run"),
        )

        result = actions.open_dashboard(app=True)

        assert result["ok"] is False
        assert result["identity_bootstrap"] is False
        assert "trusted local dashboard session" in result["detail"]

    def test_unconfirmed_extension_result_falls_back_with_bootstrap(
        self,
        monkeypatch,
    ):
        monkeypatch.setattr(
            "work_buddy.dashboard.local_identity_launch.bootstrap_fragment_for_dashboard",
            lambda app_url, *, next_hash="": "#wb-bootstrap=wbb_test",
        )
        monkeypatch.setattr(
            "work_buddy.collectors.chrome_collector.focus_or_create_tab",
            lambda *args, **kwargs: {"status": "error", "details": {}},
        )
        opened = {}
        monkeypatch.setattr(
            "webbrowser.open",
            lambda url: opened.setdefault("url", url),
        )

        result = actions.open_dashboard(app=True)

        assert result["ok"] is True
        assert result["via"] == "webbrowser"
        assert opened["url"].endswith("/app/#wb-bootstrap=wbb_test")

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

    def test_successful_restart_reconnects_an_existing_dashboard_tab(
        self,
        monkeypatch,
    ):
        from work_buddy.cli import lifecycle

        monkeypatch.setattr(
            lifecycle,
            "stop_sidecar",
            lambda: {"was_running": True, "stopped": True, "detail": "stopped"},
        )
        monkeypatch.setattr(
            lifecycle,
            "start_sidecar",
            lambda: {"started": True, "pid": 42, "detail": "started"},
        )
        monkeypatch.setattr(actions.time, "sleep", lambda _seconds: None)
        monkeypatch.setattr(
            actions,
            "reconnect_dashboard_identity",
            lambda: {
                "ok": True,
                "reconnected": True,
                "detail": "reconnected",
            },
        )

        result = actions.restart_sidecar()

        assert result["identity_reconnect"]["reconnected"] is True


class TestIdentityReconnect:
    def test_delivers_host_minted_grant_to_existing_tab(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            "work_buddy.cli.commands._wait_for_dashboard_app",
            lambda _url: True,
        )
        monkeypatch.setattr(
            "work_buddy.dashboard.local_identity_launch.bootstrap_fragment_for_dashboard",
            lambda app_url, *, next_hash="": "#wb-bootstrap=wbb_test",
        )

        def fake_focus(url, target_hash="", timeout_seconds=15):
            seen.update(
                url=url,
                target_hash=target_hash,
                timeout_seconds=timeout_seconds,
            )
            return {
                "status": "ok",
                "details": {"found": True, "created": False, "focused": False},
            }

        monkeypatch.setattr(
            "work_buddy.collectors.chrome_collector.focus_existing_tab",
            fake_focus,
        )

        result = actions.reconnect_dashboard_identity()

        assert result["ok"] is True
        assert result["reconnected"] is True
        assert seen == {
            "url": "http://127.0.0.1:5127/app/",
            "target_hash": "#wb-bootstrap=wbb_test",
            "timeout_seconds": 10,
        }

    def test_does_not_claim_reconnect_without_correlated_confirmation(
        self,
        monkeypatch,
    ):
        monkeypatch.setattr(
            "work_buddy.cli.commands._wait_for_dashboard_app",
            lambda _url: True,
        )
        monkeypatch.setattr(
            "work_buddy.dashboard.local_identity_launch.bootstrap_fragment_for_dashboard",
            lambda app_url, *, next_hash="": "#wb-bootstrap=wbb_test",
        )
        monkeypatch.setattr(
            "work_buddy.collectors.chrome_collector.focus_existing_tab",
            lambda *args, **kwargs: {},
        )
        monkeypatch.setattr(
            "work_buddy.collectors.chrome_collector.focus_or_create_tab",
            lambda *args, **kwargs: {},
        )
        monkeypatch.setattr("webbrowser.open", lambda _url: False)

        result = actions.reconnect_dashboard_identity()

        assert result["ok"] is False
        assert result["reconnected"] is False
        assert "Neither the browser extension" in result["detail"]

    def test_uses_fresh_browser_grant_when_extension_is_unavailable(
        self,
        monkeypatch,
    ):
        monkeypatch.setattr(
            "work_buddy.cli.commands._wait_for_dashboard_app",
            lambda _url: True,
        )
        minted = []

        def mint(app_url, *, next_hash=""):
            token = f"#wb-bootstrap=wbb_{len(minted) + 1}"
            minted.append(token)
            return token

        monkeypatch.setattr(
            "work_buddy.dashboard.local_identity_launch.bootstrap_fragment_for_dashboard",
            mint,
        )
        monkeypatch.setattr(
            "work_buddy.collectors.chrome_collector.focus_existing_tab",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            "work_buddy.collectors.chrome_collector.focus_or_create_tab",
            lambda *args, **kwargs: None,
        )
        opened = {}
        monkeypatch.setattr(
            "webbrowser.open",
            lambda url: opened.setdefault("url", url),
        )

        result = actions.reconnect_dashboard_identity()

        assert result["ok"] is True
        assert result["reconnected"] is True
        assert minted == [
            "#wb-bootstrap=wbb_1",
            "#wb-bootstrap=wbb_2",
            "#wb-bootstrap=wbb_3",
        ]
        assert opened["url"].endswith("/app/#wb-bootstrap=wbb_3")

    def test_confirmed_missing_tab_continues_to_create_a_trusted_session(
        self,
        monkeypatch,
    ):
        monkeypatch.setattr(
            "work_buddy.cli.commands._wait_for_dashboard_app",
            lambda _url: True,
        )
        monkeypatch.setattr(
            "work_buddy.dashboard.local_identity_launch.bootstrap_fragment_for_dashboard",
            lambda app_url, *, next_hash="": "#wb-bootstrap=wbb_test",
        )
        monkeypatch.setattr(
            "work_buddy.collectors.chrome_collector.focus_existing_tab",
            lambda *args, **kwargs: {
                "status": "ok",
                "details": {"found": False, "created": False, "focused": False},
            },
        )
        fallback = Mock(
            return_value={
                "status": "ok",
                "details": {"created": True, "focused": False},
            }
        )
        monkeypatch.setattr(
            "work_buddy.collectors.chrome_collector.focus_or_create_tab",
            fallback,
        )

        result = actions.reconnect_dashboard_identity()

        assert result["ok"] is True
        assert result["reconnected"] is True
        fallback.assert_called_once()

    def test_falls_back_when_focus_existing_tab_is_unsupported(self, monkeypatch):
        monkeypatch.setattr(
            "work_buddy.cli.commands._wait_for_dashboard_app",
            lambda _url: True,
        )
        monkeypatch.setattr(
            "work_buddy.dashboard.local_identity_launch.bootstrap_fragment_for_dashboard",
            lambda app_url, *, next_hash="": "#wb-bootstrap=wbb_test",
        )
        monkeypatch.setattr(
            "work_buddy.collectors.chrome_collector.focus_existing_tab",
            lambda *args, **kwargs: {
                "status": "error",
                "details": {"error": "Unknown mutation: focus_existing_tab"},
            },
        )
        seen = {}

        def prior_worker_focus(
            url,
            target_hash="",
            preserve_path=False,
            timeout_seconds=15,
        ):
            seen.update(
                url=url,
                target_hash=target_hash,
                preserve_path=preserve_path,
                timeout_seconds=timeout_seconds,
            )
            return {
                "status": "ok",
                "details": {"created": False, "focused": True},
            }

        monkeypatch.setattr(
            "work_buddy.collectors.chrome_collector.focus_or_create_tab",
            prior_worker_focus,
        )

        result = actions.reconnect_dashboard_identity()

        assert result["ok"] is True
        assert result["reconnected"] is True
        assert seen == {
            "url": "http://127.0.0.1:5127/app/",
            "target_hash": "#wb-bootstrap=wbb_test",
            "preserve_path": True,
            "timeout_seconds": 10,
        }
