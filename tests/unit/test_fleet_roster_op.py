"""Tests for the fleet_roster capability dispatch (config.local.yaml writer).

Monkeypatches the config read/write seam so no real config file is touched.
Hardware is multi-GPU: ``gpus`` is a list of {name, vram_gb}.
"""
from __future__ import annotations

from work_buddy.mcp_server.ops.inference_ops import _fleet_roster_dispatch


def _fake_config(monkeypatch, initial_fleet):
    """Wire read_config_local/write_config_local to an in-memory store."""
    store = {"inference": {"fleet": list(initial_fleet)}} if initial_fleet is not None else {}
    monkeypatch.setattr(
        "work_buddy.config.read_config_local",
        lambda: {k: v for k, v in store.items()},
    )
    monkeypatch.setattr(
        "work_buddy.config.write_config_local",
        lambda key, val: store.__setitem__(key, val),
    )
    return store


def _fleet(store):
    return store["inference"]["fleet"]


def test_set_new_entry_role_only(monkeypatch):
    store = _fake_config(monkeypatch, [])
    r = _fleet_roster_dispatch(action="set", device_id="DEV1", role="compute node")
    assert r["success"] is True and r["action"] == "set"
    assert _fleet(store) == [{"device_id": "DEV1", "role": "compute node"}]


def test_set_gpus_list(monkeypatch):
    store = _fake_config(monkeypatch, [])
    r = _fleet_roster_dispatch(action="set", device_id="DEV1",
                               gpus=[{"name": "RTX 4090", "vram_gb": "24"},
                                     {"name": "RTX 4090", "vram_gb": 24}], ram_gb="64")
    assert r["success"] is True
    entry = _fleet(store)[0]
    assert entry["gpus"] == [{"name": "RTX 4090", "vram_gb": 24},
                             {"name": "RTX 4090", "vram_gb": 24}]
    assert entry["ram_gb"] == 64


def test_set_partial_preserves_existing_gpus(monkeypatch):
    store = _fake_config(monkeypatch, [
        {"device_id": "DEV1", "role": "old", "gpus": [{"name": "RTX 4090", "vram_gb": 24}]},
    ])
    r = _fleet_roster_dispatch(action="set", device_id="DEV1", role="new")  # gpus omitted
    assert r["success"] is True
    entry = _fleet(store)[0]
    assert entry["role"] == "new"
    assert entry["gpus"] == [{"name": "RTX 4090", "vram_gb": 24}]  # untouched


def test_set_empty_gpus_clears(monkeypatch):
    store = _fake_config(monkeypatch, [
        {"device_id": "DEV1", "gpus": [{"name": "RTX 4090", "vram_gb": 24}]},
    ])
    r = _fleet_roster_dispatch(action="set", device_id="DEV1", gpus=[])
    assert r["success"] is True
    assert "gpus" not in _fleet(store)[0]


def test_set_gpus_migrates_legacy_scalar(monkeypatch):
    store = _fake_config(monkeypatch, [
        {"device_id": "DEV1", "gpu": "OLD", "vram_gb": 8, "role": "r"},
    ])
    r = _fleet_roster_dispatch(action="set", device_id="DEV1",
                               gpus=[{"name": "RTX 4090", "vram_gb": 24}])
    assert r["success"] is True
    entry = _fleet(store)[0]
    assert entry["gpus"] == [{"name": "RTX 4090", "vram_gb": 24}]
    assert "gpu" not in entry and "vram_gb" not in entry  # legacy keys removed
    assert entry["role"] == "r"  # untouched


def test_set_bad_gpu_vram_is_field_error(monkeypatch):
    store = _fake_config(monkeypatch, [])
    r = _fleet_roster_dispatch(action="set", device_id="DEV1",
                               gpus=[{"name": "X", "vram_gb": "lots"}])
    assert r["success"] is False and "gpus" in r["errors_by_field"]
    assert _fleet(store) == []  # nothing written on validation failure


def test_set_bad_ram_is_field_error(monkeypatch):
    store = _fake_config(monkeypatch, [])
    r = _fleet_roster_dispatch(action="set", device_id="DEV1", ram_gb="lots")
    assert r["success"] is False and "ram_gb" in r["errors_by_field"]
    assert _fleet(store) == []


def test_set_skips_blank_gpu_rows(monkeypatch):
    store = _fake_config(monkeypatch, [])
    r = _fleet_roster_dispatch(action="set", device_id="DEV1",
                               gpus=[{"name": "", "vram_gb": ""}, {"name": "RTX 4090"}])
    assert r["success"] is True
    assert _fleet(store)[0]["gpus"] == [{"name": "RTX 4090"}]


def test_empty_role_clears_but_keeps_entry(monkeypatch):
    store = _fake_config(monkeypatch, [
        {"device_id": "DEV1", "role": "old", "gpus": [{"name": "X", "vram_gb": 8}]},
    ])
    r = _fleet_roster_dispatch(action="set", device_id="DEV1", role="")
    assert r["success"] is True
    entry = _fleet(store)[0]
    assert "role" not in entry and entry["gpus"] == [{"name": "X", "vram_gb": 8}]


def test_remove_existing(monkeypatch):
    store = _fake_config(monkeypatch, [{"device_id": "DEV1", "role": "x"}])
    r = _fleet_roster_dispatch(action="remove", device_id="DEV1")
    assert r["success"] is True and _fleet(store) == []


def test_remove_missing_is_ok(monkeypatch):
    _fake_config(monkeypatch, [])
    r = _fleet_roster_dispatch(action="remove", device_id="NOPE")
    assert r["success"] is True and "No roster entry" in r["note"]


def test_missing_device_id_errors(monkeypatch):
    _fake_config(monkeypatch, [])
    r = _fleet_roster_dispatch(action="set", device_id="")
    assert r["success"] is False and "device_id" in r["errors_by_field"]


def test_unknown_action_errors(monkeypatch):
    _fake_config(monkeypatch, [])
    r = _fleet_roster_dispatch(action="frobnicate", device_id="DEV1")
    assert r["success"] is False and "error" in r


def test_preserves_other_inference_keys(monkeypatch):
    # A user may also have inference.profiles in local config — don't clobber it.
    store = {"inference": {"profiles": {"p": 1}, "fleet": []}}
    monkeypatch.setattr("work_buddy.config.read_config_local", lambda: {k: v for k, v in store.items()})
    monkeypatch.setattr("work_buddy.config.write_config_local", lambda key, val: store.__setitem__(key, val))
    r = _fleet_roster_dispatch(action="set", device_id="DEV1", role="x")
    assert r["success"] is True
    assert store["inference"]["profiles"] == {"p": 1}  # preserved
