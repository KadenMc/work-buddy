"""Unit tests for the retry/result-size robustness PR.

Covers two linked fixes:
- Issue 1: universal capability-result cap + on-demand retrieval.
- Issue 2: idempotency entry survives a consent-delayed retry (refresh-on-replay).
"""

from __future__ import annotations

import json
import time

import pytest


# ---------------------------------------------------------------------------
# Issue 1 — capability-result cap + retrieval
# ---------------------------------------------------------------------------

def test_cap_capability_result_passthrough_small():
    from work_buddy.mcp_server.tools import gateway

    small = {"a": 1, "b": "x" * 50}
    assert gateway._cap_capability_result(small, "op_test") is small


def test_cap_capability_result_truncates_oversized():
    from work_buddy.mcp_server.tools import gateway

    big = {"blob": "z" * (gateway._capability_result_cap() + 10)}
    out = gateway._cap_capability_result(big, "op_big")
    assert out["_truncated"] is True
    assert out["_operation_id"] == "op_big"
    assert out["_size"] > gateway._capability_result_cap()
    assert "wb_capability_result" in out["_message"]
    assert out["_keys"] == ["blob"]


def test_capability_result_payload_roundtrip(tmp_path, monkeypatch):
    from work_buddy.mcp_server.tools import gateway

    monkeypatch.setattr(gateway, "_get_operations_dir", lambda: tmp_path)
    full = {"items": list(range(5)), "note": "hello"}
    (tmp_path / "op_x.json").write_text(
        json.dumps({"operation_id": "op_x", "result": full}), encoding="utf-8",
    )

    # Whole result (small → returned in full).
    whole = gateway._capability_result_payload("op_x", None)
    assert whole["result"] == full

    # By key.
    byk = gateway._capability_result_payload("op_x", "note")
    assert byk["value"] == "hello"

    # Missing key → available_keys listed.
    miss = gateway._capability_result_payload("op_x", "nope")
    assert "available_keys" in miss and "items" in miss["available_keys"]

    # Unknown op.
    assert "error" in gateway._capability_result_payload("op_absent", None)


def test_capability_result_payload_caps_large_whole(tmp_path, monkeypatch):
    from work_buddy.mcp_server.tools import gateway

    monkeypatch.setattr(gateway, "_get_operations_dir", lambda: tmp_path)
    big = {"blob": "z" * (gateway._capability_result_cap() + 10)}
    (tmp_path / "op_b.json").write_text(
        json.dumps({"operation_id": "op_b", "result": big}), encoding="utf-8",
    )
    out = gateway._capability_result_payload("op_b", None)
    assert out["_truncated"] is True
    # ...but the oversized value is retrievable by its key.
    byk = gateway._capability_result_payload("op_b", "blob")
    assert byk["_truncated"] is True  # single key also over cap
    assert byk["_size"] > gateway._capability_result_cap()


# ---------------------------------------------------------------------------
# Issue 2 — idempotency survives a consent-delayed retry
# ---------------------------------------------------------------------------

def test_refresh_idempotency_revives_expired_entry(tmp_path, monkeypatch):
    from work_buddy.obsidian.tasks import mutations as m

    monkeypatch.setattr(m, "_idempotency_dir", lambda: tmp_path)

    params = {"task_text": "ZZZ-test", "summary": "s", "project": "work-buddy"}
    key = m._create_task_idempotency_key(
        task_text="ZZZ-test", summary="s", project="work-buddy",
        urgency="medium", contract=None, tags=[], due_date=None,
    )
    m._record_idempotent_create_ids(key, "t-aaaa", "uuid-bbbb")

    # Simulate the entry aging past the TTL (consent wait).
    cache_file = tmp_path / f"{key}.json"
    data = json.loads(cache_file.read_text())
    data["ts"] = time.time() - (m._IDEMPOTENCY_TTL_SEC + 60)
    cache_file.write_text(json.dumps(data))

    # Expired → resolve misses (would mint a fresh UUID → orphan).
    assert m._resolve_idempotent_create_ids(key) == (None, None)

    # The replay hook re-stamps it (ignoring expiry) → resolve hits again.
    m.refresh_idempotency_on_replay("task_create", params)
    assert m._resolve_idempotent_create_ids(key) == ("t-aaaa", "uuid-bbbb")


def test_refresh_idempotency_noop_for_other_capability(tmp_path, monkeypatch):
    from work_buddy.obsidian.tasks import mutations as m

    monkeypatch.setattr(m, "_idempotency_dir", lambda: tmp_path)
    # No file, non-create capability → must not raise.
    m.refresh_idempotency_on_replay("task_toggle", {"task_id": "t-x"})
    m.refresh_idempotency_on_replay("task_create", {"task_text": "none-cached"})
