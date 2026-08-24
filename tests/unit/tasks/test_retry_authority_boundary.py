from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from work_buddy.tasks import runtime
from work_buddy.tasks import store as native_store
from work_buddy.tasks.store import TaskStore


@pytest.fixture()
def native_runtime(tmp_path, monkeypatch):
    path = tmp_path / "tasks.sqlite3"
    monkeypatch.setattr(native_store, "default_task_db_path", lambda: path)
    monkeypatch.setattr(runtime, "default_task_db_path", lambda: path)
    monkeypatch.setattr(
        runtime,
        "_canonical_default_latch_path",
        lambda: tmp_path / "task_authority_latch.json",
    )
    store = TaskStore(path)
    store.initialize()
    current = store.system_state()
    runtime.arm_native_authority_latch(
        path,
        cohort_id="retry-test",
        target_authority_epoch="native:retry-test",
        cutover_receipt_id="retry-cutover",
        armed_at=datetime.now(timezone.utc).isoformat(),
    )
    store.set_system_state(
        expected_authority_epoch=current.authority_epoch,
        authority_epoch="native:retry-test",
        updated_at=datetime.now(timezone.utc).isoformat(),
        cutover_receipt_id="retry-cutover",
        process_generation=1,
    )
    return store


@pytest.fixture()
def operation_dir(tmp_path, monkeypatch):
    from work_buddy.mcp_server.tools import gateway

    path = tmp_path / "operations"
    path.mkdir()
    monkeypatch.setattr(gateway, "_OPERATIONS_DIR", path)
    return path


def _write_failed_task_operation(operation_dir, *, epoch, carrier=None):
    record = {
        "operation_id": "op_cutover",
        "type": "capability",
        "name": "task_create",
        "params": {"task_text": "Never replay Markdown"},
        "retry_policy": "replay",
        "status": "failed",
        "result": None,
        "error": "old failure",
        "attempt": 1,
        "locked_until": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "task_authority_epoch": epoch,
    }
    if carrier is not None:
        record["pwu_carrier"] = carrier
    (operation_dir / "op_cutover.json").write_text(
        json.dumps(record), encoding="utf-8"
    )
    return record


def test_gateway_stamps_native_task_authority(
    native_runtime,
    operation_dir,
):
    from work_buddy.mcp_server.tools import gateway

    operation_id = gateway._save_operation(
        "task_toggle",
        {"task_id": "t-example"},
        "replay",
    )
    record = json.loads((operation_dir / f"{operation_id}.json").read_text())
    assert record["task_authority_epoch"] == "native:retry-test"


def test_gateway_retry_rejects_legacy_record_before_registry_dispatch(
    native_runtime,
    operation_dir,
):
    from work_buddy.mcp_server.tools import gateway

    _write_failed_task_operation(operation_dir, epoch="legacy")
    with patch.object(gateway.registry, "get_entry") as get_entry:
        result = gateway.retry_operation("op_cutover")

    assert result["code"] == "task_replay_authority_mismatch"
    get_entry.assert_not_called()


@pytest.mark.parametrize("epoch", ["legacy", None])
def test_sidecar_never_preverifies_legacy_task_effect_after_cutover(
    native_runtime,
    operation_dir,
    epoch,
):
    from work_buddy.sidecar.retry_sweep import RetrySweep

    record = _write_failed_task_operation(
        operation_dir,
        epoch=epoch,
        carrier={
            "path": "tasks/master-task-list.md",
            "content_hint": "legacy witness",
            "write_mode": "insert",
        },
    )
    sweep = RetrySweep()
    with patch.object(sweep, "_pre_verify_pwu") as preverify:
        result = sweep._replay(record)

    assert result["success"] is False
    assert result["error_code"] == "task_replay_authority_mismatch"
    preverify.assert_not_called()


def test_sidecar_rejects_markdown_carrier_even_on_native_record(
    native_runtime,
    operation_dir,
):
    from work_buddy.sidecar.retry_sweep import RetrySweep

    record = _write_failed_task_operation(
        operation_dir,
        epoch="native:retry-test",
        carrier={
            "path": "tasks/notes/old.md",
            "content_hint": "legacy witness",
            "write_mode": "insert",
        },
    )
    sweep = RetrySweep()
    with patch.object(sweep, "_pre_verify_pwu") as preverify:
        result = sweep._replay(record)

    assert result["success"] is False
    assert result["error_code"] == "task_legacy_effect_retired"
    preverify.assert_not_called()


def test_obsidian_retry_rejects_native_task_without_bridge_probe(
    native_runtime,
    operation_dir,
):
    from work_buddy.obsidian.retry import obsidian_retry

    _write_failed_task_operation(operation_dir, epoch="native:retry-test")
    with patch("work_buddy.obsidian.bridge.is_available") as is_available:
        result = obsidian_retry("op_cutover", wait_seconds=0)

    assert result["retired"] is True
    assert result["error_code"] == "task_obsidian_retry_retired"
    is_available.assert_not_called()


def test_native_pwu_guard_does_not_resolve_markdown_effects(native_runtime):
    from work_buddy.mcp_server.tools import gateway

    assert gateway._native_task_effect_verification_retired(
        "task_create", {"task_text": "native"}
    )
    with patch(
        "work_buddy.obsidian.tasks.mutations.create_task_effects_resolver",
        MagicMock(side_effect=AssertionError("legacy resolver")),
    ):
        assert gateway._native_task_effect_verification_retired(
            "task_create", {"task_text": "native"}
        )
