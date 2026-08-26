"""The scheduler's Journal composition never starts a model or an HTTP service."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from work_buddy.journal_capture import proposal_maintenance as subject


@pytest.mark.parametrize("limit", [True, 0, 101, "10", None])
def test_rejects_invalid_bound_before_opening_stores(limit):
    with pytest.raises(ValueError, match="limit"):
        subject.reconcile_journal_task_proposals(limit=limit)


def test_restore_fence_does_not_touch_injected_service(monkeypatch):
    monkeypatch.setattr(subject, "source_foundation_read_only", lambda: True)
    service = SimpleNamespace(reconcile_proposals=Mock())
    assert subject.reconcile_journal_task_proposals(service=service) == {
        "ok": False, "code": "source_foundation_read_only"
    }
    service.reconcile_proposals.assert_not_called()


def test_passes_the_exact_bound_without_creating_another_service(monkeypatch):
    monkeypatch.setattr(subject, "source_foundation_read_only", lambda: False)
    construct = Mock(side_effect=AssertionError("must use existing service"))
    monkeypatch.setattr(subject, "JournalCaptureStore", construct)
    service = SimpleNamespace(reconcile_proposals=Mock(return_value={
        "delivery_checked": 2, "resolution_checked": 3, "resolution_synced": 1
    }))
    assert subject.reconcile_journal_task_proposals(limit=7, service=service) == {
        "ok": True, "delivery_checked": 2, "resolution_checked": 3, "resolution_synced": 1
    }
    service.reconcile_proposals.assert_called_once_with(limit=7)
    construct.assert_not_called()
