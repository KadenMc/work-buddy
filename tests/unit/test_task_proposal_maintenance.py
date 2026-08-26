from __future__ import annotations

from importlib import reload
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from work_buddy.backups import source_foundation_restore
from work_buddy.mcp_server.ops import task_proposal_ops as subject


@pytest.fixture
def maintenance(monkeypatch):
    from work_buddy import config

    state = {"dashboard": {"read_only": False}}
    monkeypatch.setattr(config, "load_config", lambda: state)
    fence = Mock()
    monkeypatch.setattr(
        source_foundation_restore, "require_source_foundation_writable", fence
    )
    calls = []

    def threads(limit):
        calls.append(("threads", limit))
        return {"ok": True, "examined": 1, "realized": 1}

    def journal(limit):
        calls.append(("journal", limit))
        return {"ok": True, "examined": 1, "synchronized": 1}

    monkeypatch.setattr(subject, "_reconcile_threads", threads)
    monkeypatch.setattr(subject, "_reconcile_journal", journal)
    return SimpleNamespace(state=state, fence=fence, calls=calls)


def test_bounded_threads_stage_precedes_journal_realization_sync(maintenance):
    result = subject.task_proposals_reconcile(limit=17)
    assert result["ok"] is True
    assert result["limit"] == 17
    assert result["threads"]["realized"] == 1
    assert result["journal"]["synchronized"] == 1
    assert maintenance.calls == [("threads", 17), ("journal", 17)]
    assert maintenance.fence.call_count == 2


@pytest.mark.parametrize("limit", [0, -1, 101, "50", True, None])
def test_invalid_limits_do_not_open_domain_write_paths(maintenance, limit):
    result = subject.task_proposals_reconcile(limit=limit)
    assert result["ok"] is False
    assert result["code"] == "invalid_limit"
    assert maintenance.calls == []
    maintenance.fence.assert_not_called()


def test_read_only_skips_all_reconciliation_without_opening_stores(maintenance):
    maintenance.state["dashboard"]["read_only"] = True
    result = subject.task_proposals_reconcile()
    assert result["code"] == "dashboard_read_only"
    assert result["skipped"] is True
    assert maintenance.calls == []
    maintenance.fence.assert_not_called()


def test_restore_fence_skips_all_reconciliation(maintenance):
    maintenance.fence.side_effect = (
        source_foundation_restore.SourceFoundationRestorePending("test")
    )
    result = subject.task_proposals_reconcile()
    assert result["code"] == "source_foundation_restore_pending"
    assert result["ok"] is False
    assert maintenance.calls == []


def test_pause_is_rechecked_before_journal_writes(maintenance, monkeypatch):
    def threads(limit):
        maintenance.calls.append(("threads", limit))
        maintenance.state["dashboard"]["read_only"] = True
        return {"ok": True, "examined": 1}

    monkeypatch.setattr(subject, "_reconcile_threads", threads)
    result = subject.task_proposals_reconcile(limit=3)
    assert result["code"] == "dashboard_read_only"
    assert result["threads"]["ok"] is True
    assert result["journal"]["skipped"] is True
    assert maintenance.calls == [("threads", 3)]


def test_stage_failure_does_not_strand_journal_or_leak_content(
    maintenance, monkeypatch, caplog
):
    def threads(limit):
        maintenance.calls.append(("threads", limit))
        raise RuntimeError("sensitive task text from a provider")

    monkeypatch.setattr(subject, "_reconcile_threads", threads)
    result = subject.task_proposals_reconcile(limit=9)
    assert result["ok"] is False
    assert result["threads"]["code"] == "reconciliation_unavailable"
    assert result["journal"]["ok"] is True
    assert maintenance.calls == [("threads", 9), ("journal", 9)]
    assert "sensitive task text" not in str(result) + caplog.text


def test_journal_failure_does_not_claim_success(maintenance, monkeypatch):
    monkeypatch.setattr(
        subject,
        "_reconcile_journal",
        lambda _limit: {"ok": False, "code": "capture_store_unavailable"},
    )
    result = subject.task_proposals_reconcile()
    assert result["ok"] is False
    assert result["threads"]["ok"] is True
    assert result["journal"]["code"] == "capture_store_unavailable"


def test_thread_summary_is_bounded_and_does_not_include_task_content(monkeypatch):
    from work_buddy.threads import action_proposals

    reconcile = Mock(
        return_value=[
            {
                "proposal": {
                    "status": "realized",
                    "parameters": {"task_text": "private"},
                }
            },
            {"proposal": {"status": "needs_attention"}},
            {"proposal": {"status": "unavailable"}},
        ]
    )
    monkeypatch.setattr(
        action_proposals,
        "get_action_proposal_service",
        lambda: SimpleNamespace(reconcile_pending=reconcile),
    )
    result = subject._reconcile_threads(3)
    reconcile.assert_called_once_with(limit=3)
    assert result == {
        "ok": False,
        "examined": 3,
        "realized": 1,
        "needs_attention": 1,
        "unavailable": 1,
        "pending": 2,
    }
    assert "private" not in str(result)


def test_journal_stage_delegates_only_to_bounded_domain_maintenance(monkeypatch):
    from work_buddy.journal_capture import proposal_maintenance

    reconcile = Mock(return_value={"ok": True, "examined": 5})
    monkeypatch.setattr(
        proposal_maintenance, "reconcile_journal_task_proposals", reconcile
    )
    assert subject._reconcile_journal(11) == {"ok": True, "examined": 5}
    reconcile.assert_called_once_with(limit=11)


def test_capability_registers_without_opening_domain_stores():
    from work_buddy.mcp_server.op_registry import get_op

    reloaded = reload(subject)
    assert get_op("op.wb.task_proposals_reconcile") is reloaded.task_proposals_reconcile
