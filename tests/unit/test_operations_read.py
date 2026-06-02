"""Read-only operation-record reader for shell-level pollers.

Proves ``work_buddy.operations_read`` mirrors the gateway's on-disk
operation layout without importing the gateway, and maps record states to
the CLI's normalized vocabulary (including stale-lease detection).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from work_buddy import operations_read


def _write_op(ops_dir, op_id, **fields):
    rec = {
        "operation_id": op_id,
        "name": "do_thing",
        "status": "running",
        "error": None,
        "error_kind": None,
        "created_at": "2026-06-01T00:00:00+00:00",
        "completed_at": None,
        "locked_until": None,
    }
    rec.update(fields)
    ops_dir.mkdir(parents=True, exist_ok=True)
    (ops_dir / f"{op_id}.json").write_text(json.dumps(rec), encoding="utf-8")


def test_load_missing_returns_none(tmp_agents_dir):
    assert operations_read.load_operation("op_missing") is None


def test_status_not_found(tmp_agents_dir):
    s = operations_read.operation_status("op_missing")
    assert s["state"] == "not_found"
    assert s["terminal"] is False


def test_status_completed(tmp_agents_dir):
    ops = operations_read.operations_dir()
    _write_op(ops, "op_done", status="completed",
              completed_at="2026-06-01T01:00:00+00:00", result={"ok": True})
    s = operations_read.operation_status("op_done")
    assert s["state"] == "completed"
    assert s["terminal"] is True
    assert s["name"] == "do_thing"
    assert s["completed_at"] == "2026-06-01T01:00:00+00:00"


def test_status_failed(tmp_agents_dir):
    ops = operations_read.operations_dir()
    _write_op(ops, "op_fail", status="failed", error="boom", error_kind="x")
    s = operations_read.operation_status("op_fail")
    assert s["state"] == "failed"
    assert s["terminal"] is True
    assert s["error"] == "boom"
    assert s["error_kind"] == "x"


def test_status_running_with_live_lease_is_pending(tmp_agents_dir):
    ops = operations_read.operations_dir()
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    _write_op(ops, "op_run", status="running", locked_until=future)
    s = operations_read.operation_status("op_run")
    assert s["state"] == "running"
    assert s["terminal"] is False


def test_status_running_with_expired_lease_is_stale(tmp_agents_dir):
    ops = operations_read.operations_dir()
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    _write_op(ops, "op_stale", status="running", locked_until=past)
    s = operations_read.operation_status("op_stale")
    assert s["state"] == "stale"
    assert s["terminal"] is False  # not terminal — a long lease may be legit


def test_running_no_lease_is_pending(tmp_agents_dir):
    ops = operations_read.operations_dir()
    _write_op(ops, "op_nolease", status="running", locked_until=None)
    assert operations_read.operation_status("op_nolease")["state"] == "running"


def test_unreadable_record_returns_none(tmp_agents_dir):
    ops = operations_read.operations_dir()
    ops.mkdir(parents=True, exist_ok=True)
    (ops / "op_bad.json").write_text("{not json", encoding="utf-8")
    assert operations_read.load_operation("op_bad") is None
    assert operations_read.operation_status("op_bad")["state"] == "not_found"
