"""Unit tests for ``enqueue_capability_for_retry`` — the public seam that lets
out-of-band callers (e.g. the Telegram capture handler, which runs in a
separate sidecar process and does NOT dispatch through ``wb_run``) enqueue a
transiently-failed operation for the sidecar retry sweep instead of dropping
it. The sweep replays the capability from the registry on backoff, so a
capture blocked by a busy editor (409 editor_dirty) lands once the bridge frees
up rather than being lost.
"""
from __future__ import annotations

import json
from unittest.mock import patch

from work_buddy.mcp_server.tools.gateway import enqueue_capability_for_retry


class _FakeEntry:
    retry_policy = "verify_first"


class _FakeRegistry:
    def get(self, name):
        return _FakeEntry()


def _read_record(ops_dir, op_id):
    return json.loads((ops_dir / f"{op_id}.json").read_text(encoding="utf-8"))


def test_enqueue_creates_queued_op_record(tmp_path):
    params = {
        "content": "captured text",
        "note": "latest_journal",
        "section": "Running Notes",
        "position": "top",
        "source": None,
    }
    with patch(
        "work_buddy.mcp_server.tools.gateway._get_operations_dir",
        return_value=tmp_path,
    ), patch(
        "work_buddy.mcp_server.registry.get_registry",
        return_value=_FakeRegistry(),
    ):
        op_id = enqueue_capability_for_retry(
            "vault_write_at_location",
            params,
            error="editor_dirty: journal/2026-06-01.md",
            error_kind="obsidian_editor_conflict",
        )
        assert op_id is not None
        record = _read_record(tmp_path, op_id)

    # Replays the exact capability + params the caller failed on.
    assert record["name"] == "vault_write_at_location"
    assert record["params"] == params
    # Marked failed + queued so the sweep's _is_ready picks it up.
    assert record["status"] == "failed"
    assert record["queued"] is True
    assert record["queue_reason"] == "retry"
    assert record["error_class"] == "transient"
    assert record["error_kind"] == "obsidian_editor_conflict"
    # Carries the capability's declared replay policy.
    assert record["retry_policy"] == "verify_first"
    # A scheduled retry time is set in the future.
    assert record["retry_at"]


def test_enqueue_returns_none_when_persistence_fails(tmp_path):
    """Best-effort: if the op record can't be persisted, return None so the
    caller can surface the original error instead of claiming a queued retry."""
    with patch(
        "work_buddy.mcp_server.tools.gateway._save_operation",
        side_effect=OSError("disk full"),
    ):
        op_id = enqueue_capability_for_retry(
            "vault_write_at_location",
            {"content": "x"},
            error="boom",
        )
    assert op_id is None
