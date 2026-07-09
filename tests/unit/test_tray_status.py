"""Tray status model: health/busy classification and the pure UI helpers.

All Qt-free by design: the tray's classification is the SAME shared helpers
the CLI uses (``lifecycle.daemon_health`` / ``lifecycle.dispatch_busy``), and
the tooltip / icon-key / menu-enabled logic lives in ``tray.status`` precisely
so it is testable without a QApplication.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from work_buddy.cli import lifecycle
from work_buddy.tray import status as tray_status
from work_buddy.tray.status import TrayStatus, icon_key, menu_enabled, tooltip


def _svc(status):
    return SimpleNamespace(status=status, port=None, crash_count=0)


def _state(**kw):
    base = dict(
        pid=1234,
        started_at=time.time() - 600,
        last_tick_at=time.time(),
        dispatch_phase="idle",
        dispatch_phase_since=None,
        dispatch_job=None,
        last_dispatch_at=None,
        services={},
    )
    base.update(kw)
    return SimpleNamespace(**base)


class TestDaemonHealthPublicName:
    def test_public_alias_exists(self):
        assert lifecycle.daemon_health is lifecycle._daemon_health

    def test_down_when_no_pid(self):
        assert lifecycle.daemon_health(None, None) == "down"

    def test_up_when_state_fresh(self):
        assert lifecycle.daemon_health(1234, _state()) == "up"

    def test_booting_within_grace(self, monkeypatch):
        monkeypatch.setattr(lifecycle, "_pid_file_age_s", lambda: 10.0)
        assert lifecycle.daemon_health(1234, None) == "booting"

    def test_wedged_past_grace(self, monkeypatch):
        monkeypatch.setattr(lifecycle, "_pid_file_age_s", lambda: 999.0)
        stale = _state(last_tick_at=time.time() - 600, started_at=time.time() - 900)
        assert lifecycle.daemon_health(1234, stale) == "wedged"


class TestDispatchBusy:
    def test_none_state(self):
        assert lifecycle.dispatch_busy(None) is None

    def test_idle(self):
        assert lifecycle.dispatch_busy(_state()) is None

    def test_short_busy_not_noteworthy(self):
        st = _state(dispatch_phase="jobs", dispatch_phase_since=time.time() - 30)
        assert lifecycle.dispatch_busy(st) is None

    def test_long_busy_reports_phase_and_job(self):
        st = _state(
            dispatch_phase="jobs",
            dispatch_phase_since=time.time() - 300,
            dispatch_job="morning-routine",
        )
        busy = lifecycle.dispatch_busy(st)
        assert busy is not None
        assert busy["phase"] == "jobs"
        assert busy["job"] == "morning-routine"
        assert busy["busy_for_s"] >= 299

    def test_state_without_dispatch_fields(self):
        assert lifecycle.dispatch_busy(SimpleNamespace(pid=1)) is None


class TestReadStatusServiceMap:
    """read_status exposes a name->status map for the panel's per-service list."""

    def test_service_map_when_up(self, monkeypatch):
        from work_buddy.cli import lifecycle
        from work_buddy.sidecar import pid as _pid
        from work_buddy.sidecar import state as _state_mod

        st = _state(services={"messaging": _svc("healthy"), "embedding": _svc("crashed")})
        monkeypatch.setattr(_pid, "check_existing_daemon", lambda: 1234)
        monkeypatch.setattr(_state_mod, "load_state", lambda: st)
        monkeypatch.setattr(lifecycle, "daemon_health", lambda p, s: "up")

        result = tray_status.read_status()
        assert result.services == {"messaging": "healthy", "embedding": "crashed"}
        assert result.services_total == 2
        assert result.services_healthy == 1

    def test_empty_service_map_when_down(self, monkeypatch):
        from work_buddy.cli import lifecycle
        from work_buddy.sidecar import pid as _pid
        from work_buddy.sidecar import state as _state_mod

        monkeypatch.setattr(_pid, "check_existing_daemon", lambda: None)
        monkeypatch.setattr(_state_mod, "load_state", lambda: None)
        monkeypatch.setattr(lifecycle, "daemon_health", lambda p, s: "down")

        result = tray_status.read_status()
        assert result.services == {}
        assert result.services_total == 0


class TestIconKeyAndTooltip:
    def _status(self, health, busy=None, healthy=0, total=0):
        return TrayStatus(
            health=health, busy=busy,
            services_healthy=healthy, services_total=total, pid=1,
        )

    def test_icon_key_health_passthrough(self):
        for health in ("down", "booting", "wedged", "up"):
            assert icon_key(self._status(health)) == health

    def test_icon_key_busy_overlay_only_when_up(self):
        busy = {"phase": "jobs", "job": None, "busy_for_s": 200.0}
        assert icon_key(self._status("up", busy=busy)) == "busy"

    def test_tooltips(self):
        assert "5/6 services healthy" in tooltip(self._status("up", healthy=5, total=6))
        assert "starting" in tooltip(self._status("booting"))
        assert "Restart" in tooltip(self._status("wedged"))
        assert "stopped" in tooltip(self._status("down"))

    def test_tooltip_busy_names_the_job(self):
        busy = {"phase": "jobs", "job": "morning-routine", "busy_for_s": 200.0}
        assert "morning-routine" in tooltip(self._status("up", busy=busy, healthy=5, total=5))


class TestMenuEnabled:
    def test_pending_disables_everything(self):
        assert menu_enabled("up", True) == {"start": False, "stop": False, "restart": False}

    def test_down(self):
        assert menu_enabled("down", False) == {"start": True, "stop": False, "restart": False}

    def test_up(self):
        assert menu_enabled("up", False) == {"start": False, "stop": True, "restart": True}

    def test_wedged_allows_start_takeover(self):
        e = menu_enabled("wedged", False)
        assert e["start"] and e["stop"] and e["restart"]


class TestIconAssets:
    """The Qt backend loads tray-<state>-<size>.png for every state/size pair;
    a missing file would render an empty icon silently."""

    def test_all_state_size_assets_exist(self):
        asset_dir = Path(tray_status.__file__).parent / "assets"
        for state in ("up", "booting", "wedged", "down", "busy"):
            for size in (16, 24, 32, 48):
                assert (asset_dir / f"tray-{state}-{size}.png").is_file(), (
                    f"missing tray asset: {state}-{size}"
                )
