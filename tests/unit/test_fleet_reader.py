"""Tests for the provider-neutral local model fleet reader.

The load-bearing piece is the pure ``merge_fleet`` function — fed already-parsed
provider inputs (no subprocess, no IO), so every merge rule is unit-testable with
plain dicts. Fixtures mirror real ``lms link status`` / ``lms ps`` /
``lms runtime survey`` JSON shapes. Hardware is multi-GPU: each machine carries a
list of GPUs.
"""
from __future__ import annotations

from work_buddy.inference.fleet import (
    _normalize_lms_survey,
    merge_fleet,
)


# Real-shaped fixtures (ids shortened for readability) -----------------------

LINK_STATUS = {
    "status": "online",
    "deviceIdentifier": "LOCAL",
    "deviceName": "LAPTOP-LOCAL",
    "peers": [
        {"deviceIdentifier": "PEER8", "deviceName": "peer-a",
         "status": "connected", "loadedModels": []},
        {"deviceIdentifier": "PEER12", "deviceName": "peer-b",
         "status": "connected", "loadedModels": ["text-embedding-x"]},
    ],
}

PS = [
    {"deviceIdentifier": "PEER12", "modelKey": "text-embedding-x",
     "displayName": "Embed X", "type": "embedding",
     "quantization": {"name": "Q8_0", "bits": 8}, "sizeBytes": 117852672,
     "contextLength": 512, "maxContextLength": 512, "status": "idle", "queued": 0},
]

# Canonical roster shape — gpus is a list (machines can have several).
ROSTER = [
    {"device_id": "LOCAL", "gpus": [{"name": "GTX 1650", "vram_gb": 4}], "ram_gb": 16, "role": "host"},
    {"device_id": "PEER8", "gpus": [{"name": "RTX 2080", "vram_gb": 8}], "ram_gb": 64, "role": "peer8"},
    {"device_id": "PEER12", "gpus": [{"name": "RTX 5070 Ti", "vram_gb": 12}], "ram_gb": 32, "role": "peer12"},
]

# Normalized survey shape (what _normalize_lms_survey emits): per-GPU bytes.
LOCAL_HW = {
    "gpus": [{"name": "NVIDIA GeForce GTX 1650", "vram_bytes": 4294639616}],
    "ram_bytes": 16541487104,
}

REAL_SURVEY = {
    "status": "ok",
    "engines": [{
        "name": "llama.cpp-win-x86_64-nvidia-cuda12-avx2",
        "hardwareSurvey": {
            "gpuSurveyResult": {
                "result": {"code": "success", "message": ""},
                "gpuInfo": [{"name": "NVIDIA GeForce GTX 1650",
                             "totalMemoryCapacityBytes": 4294639616}],
            },
        },
        "memoryInfo": {"ramCapacity": 16541487104, "vramCapacity": 4294639616},
    }],
}


def _by_id(snap, device_id):
    return next(m for m in snap.machines if m.device_id == device_id)


# merge_fleet -----------------------------------------------------------------

def test_local_machine_synthesized_from_top_level():
    # The local machine is the top-level identity of link status, NOT in peers[].
    snap = merge_fleet(LINK_STATUS, PS, LOCAL_HW, ROSTER)
    assert snap.local_device_id == "LOCAL"
    assert snap.machines[0].is_local is True  # local sorts first
    local = _by_id(snap, "LOCAL")
    assert local.reachable is True
    assert local.name == "LAPTOP-LOCAL"


def test_local_hardware_is_live_from_survey():
    snap = merge_fleet(LINK_STATUS, PS, LOCAL_HW, ROSTER)
    hw = _by_id(snap, "LOCAL").hardware
    assert hw.source == "live"
    assert len(hw.gpus) == 1
    assert hw.gpus[0].name == "NVIDIA GeForce GTX 1650"
    assert hw.gpus[0].vram_gb == 4.0  # 4294639616 bytes -> 4.0 GiB
    assert hw.total_vram_gb == 4.0
    assert hw.ram_gb == 15.4  # 16541487104 bytes -> 15.4 GiB


def test_peer_connected_with_zero_models():
    snap = merge_fleet(LINK_STATUS, PS, LOCAL_HW, ROSTER)
    peer8 = _by_id(snap, "PEER8")
    assert peer8.reachable is True
    assert peer8.loaded_models == []


def test_model_joined_onto_remote_peer():
    snap = merge_fleet(LINK_STATUS, PS, LOCAL_HW, ROSTER)
    peer12 = _by_id(snap, "PEER12")
    assert len(peer12.loaded_models) == 1
    lm = peer12.loaded_models[0]
    assert lm.model == "text-embedding-x"
    assert lm.kind == "embedding"
    assert lm.quant == "Q8_0"
    assert lm.context_length == 512 and lm.max_context_length == 512


def test_roster_enriches_peer_hardware():
    snap = merge_fleet(LINK_STATUS, PS, LOCAL_HW, ROSTER)
    hw = _by_id(snap, "PEER8").hardware
    assert hw.source == "roster"
    assert hw.gpus[0].name == "RTX 2080" and hw.gpus[0].vram_gb == 8
    assert hw.total_vram_gb == 8 and hw.ram_gb == 64


def test_multiple_gpus_sum_to_total():
    roster = [{"device_id": "PEER8", "gpus": [
        {"name": "RTX 4090", "vram_gb": 24},
        {"name": "RTX 4090", "vram_gb": 24},
    ], "role": "dual"}]
    snap = merge_fleet(LINK_STATUS, [], LOCAL_HW, roster)
    hw = _by_id(snap, "PEER8").hardware
    assert len(hw.gpus) == 2
    assert hw.total_vram_gb == 48


def test_roster_back_compat_scalar_gpu():
    # A legacy scalar gpu/vram_gb entry still renders as a single GPU.
    roster = [{"device_id": "PEER8", "gpu": "RTX 2080", "vram_gb": 8, "role": "legacy"}]
    snap = merge_fleet(LINK_STATUS, [], LOCAL_HW, roster)
    hw = _by_id(snap, "PEER8").hardware
    assert len(hw.gpus) == 1
    assert hw.gpus[0].name == "RTX 2080" and hw.gpus[0].vram_gb == 8


def test_discovered_but_unrostered_has_unknown_hardware():
    snap = merge_fleet(LINK_STATUS, PS, LOCAL_HW, roster=[])
    peer8 = _by_id(snap, "PEER8")
    assert peer8.discovered is True and peer8.in_roster is False
    assert peer8.hardware.source == "unknown"
    assert peer8.hardware.gpus == []


def test_offline_rostered_machine_is_shown_not_omitted():
    # PEER12 is in the roster but absent from the live link status.
    link = {
        "status": "online", "deviceIdentifier": "LOCAL", "deviceName": "LAPTOP-LOCAL",
        "peers": [{"deviceIdentifier": "PEER8", "deviceName": "peer-a",
                   "status": "connected", "loadedModels": []}],
    }
    snap = merge_fleet(link, [], LOCAL_HW, ROSTER)
    peer12 = _by_id(snap, "PEER12")
    assert peer12.reachable is False
    assert peer12.discovered is False and peer12.in_roster is True
    assert peer12.hardware.source == "roster" and peer12.hardware.gpus[0].vram_gb == 12


def test_lms_unavailable_degrades_to_offline_roster():
    snap = merge_fleet({}, [], None, ROSTER, lms_available=False, error="lms down")
    assert snap.lms_available is False and snap.error == "lms down"
    assert len(snap.machines) == 3
    assert all(not m.reachable for m in snap.machines)
    assert all(m.in_roster and not m.discovered for m in snap.machines)


def test_ps_detail_falls_back_to_bare_link_names():
    # link status lists a loaded model name, but ps has no detail for it.
    snap = merge_fleet(LINK_STATUS, ps=[], local_hardware=LOCAL_HW, roster=ROSTER)
    peer12 = _by_id(snap, "PEER12")
    assert [m.model for m in peer12.loaded_models] == ["text-embedding-x"]
    assert peer12.loaded_models[0].quant is None  # bare — no ps detail


def test_machine_ordering_local_then_reachable_then_offline():
    link = {
        "status": "online", "deviceIdentifier": "LOCAL", "deviceName": "LAPTOP-LOCAL",
        "peers": [{"deviceIdentifier": "PEER8", "deviceName": "peer-a",
                   "status": "connected", "loadedModels": []}],
    }
    snap = merge_fleet(link, [], LOCAL_HW, ROSTER)  # PEER12 offline (roster only)
    order = [m.device_id for m in snap.machines]
    assert order[0] == "LOCAL"          # local first
    assert order[1] == "PEER8"          # reachable peer
    assert order[2] == "PEER12"         # offline last


def test_to_dict_is_json_serializable():
    import json
    snap = merge_fleet(LINK_STATUS, PS, LOCAL_HW, ROSTER)
    d = snap.to_dict()
    json.dumps(d)  # must not raise
    hw = d["machines"][0]["hardware"]
    assert hw["source"] == "live"
    assert hw["gpus"][0]["name"] == "NVIDIA GeForce GTX 1650"


# _normalize_lms_survey -------------------------------------------------------

def test_normalize_survey_extracts_gpus_and_ram():
    norm = _normalize_lms_survey(REAL_SURVEY)
    assert norm["ram_bytes"] == 16541487104
    assert norm["gpus"] == [{"name": "NVIDIA GeForce GTX 1650", "vram_bytes": 4294639616}]


def test_normalize_survey_collects_multiple_gpus():
    survey = {
        "engines": [{
            "hardwareSurvey": {"gpuSurveyResult": {
                "result": {"code": "success"},
                "gpuInfo": [
                    {"name": "RTX 4090", "dedicatedMemoryCapacityBytes": 25757220864},
                    {"name": "RTX 4090", "dedicatedMemoryCapacityBytes": 25757220864},
                ],
            }},
            "memoryInfo": {"ramCapacity": 137438953472},
        }],
    }
    norm = _normalize_lms_survey(survey)
    assert len(norm["gpus"]) == 2
    assert norm["gpus"][0]["vram_bytes"] == 25757220864


def test_normalize_survey_tolerates_missing_pieces():
    assert _normalize_lms_survey({}) is None
    assert _normalize_lms_survey({"engines": []}) is None
    # GPU probe failed but memory still present -> gpus empty, ram kept.
    partial = {"engines": [{
        "hardwareSurvey": {"gpuSurveyResult": {"result": {"code": "error"}}},
        "memoryInfo": {"vramCapacity": 123, "ramCapacity": 456},
    }]}
    norm = _normalize_lms_survey(partial)
    assert norm["gpus"] == [] and norm["ram_bytes"] == 456
