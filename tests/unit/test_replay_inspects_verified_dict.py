"""Regression test for ``RetrySweep._replay`` inspecting the inner
capability's ``result["verified"]`` dict.

``create_task`` can return a response like::

    {"success": True, "task_line": "...", "verified": {"task_line": False,
     "store": True, "note": True}, ...}

when one of its side-effect writes silently failed (e.g. the master-
task-list line never landed but the note + store did). The sweep's
absence-of-``error`` success criterion is not enough — it must also
inspect ``verified`` and treat any non-``True``/``\"verified\"`` field
as a transient partial-failure so the outer sweep re-enqueues.
Capabilities with declared effects are required to be idempotent under
retry, so the next replay heals the half-state. Capabilities without a
``verified`` field are unaffected.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from work_buddy.sidecar.retry_sweep import RetrySweep


@pytest.fixture
def operations_dir(tmp_path: Path, monkeypatch) -> Path:
    """Redirect ``_get_operations_dir`` so we can persist synthetic op
    records without touching real state."""
    ops_dir = tmp_path / "operations"
    ops_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "work_buddy.sidecar.retry_sweep._get_operations_dir",
        lambda: ops_dir,
    )
    return ops_dir


def _make_fake_capability_entry(
    monkeypatch,
    name: str,
    return_value: dict[str, Any],
) -> None:
    """Patch the registry so ``reg.get(name)`` returns a stub Capability
    whose ``.callable`` returns ``return_value``."""
    from work_buddy.mcp_server.registry import Capability

    fake_entry = Capability(
        name=name,
        description="test stub",
        category="test",
        parameters={},
        callable=lambda **kw: return_value,
    )

    real_get_registry = None
    try:
        from work_buddy.mcp_server import registry as reg_mod
        real_get_registry = reg_mod.get_registry
    except Exception:  # pragma: no cover
        pass

    def _patched_registry():
        return {name: fake_entry}

    monkeypatch.setattr(
        "work_buddy.mcp_server.registry.get_registry",
        _patched_registry,
    )


def test_replay_treats_verified_false_as_partial_failure(
    operations_dir: Path, monkeypatch,
) -> None:
    """Inner result with any False in ``verified`` → re-enqueue, not
    success. ``_replay`` returns
    ``{"success": False, "transient": True}`` so the outer sweep
    schedules another retry rather than firing ``retry_success`` on a
    partial-state write.
    """
    _make_fake_capability_entry(
        monkeypatch,
        "fake_partial_cap",
        {
            "success": True,
            "task_line": "- [ ] something 🆔 t-X",
            "verified": {"task_line": False, "store": True, "note": True},
        },
    )

    record = {
        "operation_id": "op_partial_test",
        "name": "fake_partial_cap",
        "params": {},
        "status": "failed",
        "attempt": 1,
    }
    (operations_dir / f"{record['operation_id']}.json").write_text(
        json.dumps(record), encoding="utf-8"
    )

    result = RetrySweep(config={})._replay(record)

    assert result["success"] is False, (
        f"Expected partial-verification to re-enqueue, but got: {result!r}"
    )
    assert result.get("transient") is True
    assert "task_line" in (result.get("error") or "")


def test_replay_passes_when_all_verified_true(
    operations_dir: Path, monkeypatch,
) -> None:
    """All True in ``verified`` → success, same as before."""
    _make_fake_capability_entry(
        monkeypatch,
        "fake_all_verified",
        {
            "success": True,
            "verified": {"task_line": True, "store": True, "note": True},
        },
    )

    record = {
        "operation_id": "op_all_verified",
        "name": "fake_all_verified",
        "params": {},
        "status": "failed",
        "attempt": 1,
    }
    (operations_dir / f"{record['operation_id']}.json").write_text(
        json.dumps(record), encoding="utf-8"
    )

    result = RetrySweep(config={})._replay(record)

    assert result["success"] is True


def test_replay_unchanged_when_no_verified_field(
    operations_dir: Path, monkeypatch,
) -> None:
    """Capabilities that don't return a ``verified`` field are
    unaffected (most capabilities)."""
    _make_fake_capability_entry(
        monkeypatch,
        "fake_no_verified",
        {"success": True, "data": "anything"},
    )

    record = {
        "operation_id": "op_no_verified",
        "name": "fake_no_verified",
        "params": {},
        "status": "failed",
        "attempt": 1,
    }
    (operations_dir / f"{record['operation_id']}.json").write_text(
        json.dumps(record), encoding="utf-8"
    )

    result = RetrySweep(config={})._replay(record)

    assert result["success"] is True


def test_replay_verified_string_values_treated_as_failure_when_not_verified(
    operations_dir: Path, monkeypatch,
) -> None:
    """Verdict values use both legacy boolean (``True``/``False``) and
    string vocabulary (``verified | absent | indeterminate | partial``)
    depending on the capability. Any value other than ``True`` or
    ``"verified"`` is treated as a not-yet-verified effect — the helper
    accepts both shapes uniformly.
    """
    _make_fake_capability_entry(
        monkeypatch,
        "fake_string_verdicts",
        {
            "success": True,
            "verified": {"task_line": "absent", "store": "verified"},
        },
    )

    record = {
        "operation_id": "op_string_verdicts",
        "name": "fake_string_verdicts",
        "params": {},
        "status": "failed",
        "attempt": 1,
    }
    (operations_dir / f"{record['operation_id']}.json").write_text(
        json.dumps(record), encoding="utf-8"
    )

    result = RetrySweep(config={})._replay(record)

    assert result["success"] is False
    assert result.get("transient") is True
