"""Tests for the dashboard fleet read-model cache, route, and frontend module."""
from __future__ import annotations

from work_buddy.dashboard import api


def setup_function():
    api._fleet_cache = None
    api._fleet_cache_ts = 0.0
    api._fleet_refreshing = False


def test_get_fleet_summary_caches(monkeypatch):
    calls = {"n": 0}

    def _fake_build():
        calls["n"] += 1
        return {"machines": [], "lms_available": True}

    monkeypatch.setattr(api, "_build_fleet_summary", _fake_build)
    a = api.get_fleet_summary()
    b = api.get_fleet_summary()
    assert calls["n"] == 1 and a == b  # second read served from cache


def test_route_shape(monkeypatch):
    from work_buddy.dashboard.service import app
    monkeypatch.setattr(
        api, "_build_fleet_summary",
        lambda: {"machines": [{"device_id": "x", "name": "X"}],
                 "lms_available": True, "local_device_id": "x"},
    )
    resp = app.test_client().get("/api/fleet")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["lms_available"] is True
    assert body["machines"][0]["device_id"] == "x"


def test_frontend_fleet_section_is_independent_of_inference_sse():
    """The fleet section must not subscribe to the inference SSE event and must
    use the surgical morph mutator, not a wholesale innerHTML rewrite."""
    from work_buddy.dashboard.frontend.scripts.tabs import fleet
    src = fleet.script()
    assert "window.fleetSurface" in src
    assert "loadFleet" in src
    assert "_wbMorphReplace" in src
    # No SSE subscription inside the section itself — external model loads have no
    # internal event; live updates come via the central dispatcher's fleet.changed.
    assert "eventBus.on" not in src
    assert "inference.call_logged" not in src


def test_fleet_fingerprint_detects_material_change():
    base = {"lms_available": True, "machines": [
        {"device_id": "a", "reachable": True, "loaded_models": [{"model": "m1"}]},
    ]}
    same = {"lms_available": True, "machines": [
        # volatile fields differ (queued/context) but material state identical
        {"device_id": "a", "reachable": True, "loaded_models": [{"model": "m1"}]},
    ]}
    reach = {"lms_available": True, "machines": [
        {"device_id": "a", "reachable": False, "loaded_models": [{"model": "m1"}]},
    ]}
    model = {"lms_available": True, "machines": [
        {"device_id": "a", "reachable": True, "loaded_models": [{"model": "m2"}]},
    ]}
    assert api._fleet_fingerprint(base) == api._fleet_fingerprint(same)
    assert api._fleet_fingerprint(base) != api._fleet_fingerprint(reach)
    assert api._fleet_fingerprint(base) != api._fleet_fingerprint(model)


def test_fleet_roster_route_success(monkeypatch):
    from types import SimpleNamespace
    from work_buddy.dashboard.service import app
    import work_buddy.mcp_server.ops.inference_ops as iops

    store = {"inference": {"fleet": []}}
    monkeypatch.setattr("work_buddy.config.read_config_local", lambda: {k: v for k, v in store.items()})
    monkeypatch.setattr("work_buddy.config.write_config_local", lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(
        "work_buddy.mcp_server.registry.get_registry",
        lambda: {"fleet_roster": SimpleNamespace(callable=iops._fleet_roster_dispatch)},
    )
    monkeypatch.setattr(api, "bust_fleet_cache", lambda: None)
    monkeypatch.setattr("work_buddy.dashboard.events.publish_auto", lambda *a, **k: None)

    resp = app.test_client().post("/api/fleet/roster",
                                  json={"action": "set", "device_id": "X", "role": "r"})
    assert resp.status_code == 200 and resp.get_json()["success"] is True
    assert store["inference"]["fleet"][0]["role"] == "r"


def test_fleet_roster_route_validation_400(monkeypatch):
    from types import SimpleNamespace
    from work_buddy.dashboard.service import app
    import work_buddy.mcp_server.ops.inference_ops as iops

    monkeypatch.setattr("work_buddy.config.read_config_local", lambda: {})
    monkeypatch.setattr("work_buddy.config.write_config_local", lambda k, v: None)
    monkeypatch.setattr(
        "work_buddy.mcp_server.registry.get_registry",
        lambda: {"fleet_roster": SimpleNamespace(callable=iops._fleet_roster_dispatch)},
    )
    resp = app.test_client().post("/api/fleet/roster", json={"action": "set", "device_id": ""})
    assert resp.status_code == 400
    assert "device_id" in resp.get_json()["errors_by_field"]


def test_frontend_has_inline_editor_and_dispatcher_subscribes():
    from work_buddy.dashboard.frontend.scripts.tabs import fleet
    from work_buddy.dashboard.frontend.scripts.core import event_bus
    src = fleet.script()
    assert "showFleetForm" in src and "submitFleetForm" in src
    assert "/api/fleet/roster" in src
    # The central dispatcher (not the section) owns the SSE subscription.
    assert "fleet.changed" in event_bus.script()
