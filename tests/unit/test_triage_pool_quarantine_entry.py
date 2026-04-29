"""Tests for the per-entry quarantine capability.

Covers:
  - happy path: a valid (run_id, item_id) flips state to quarantined
  - idempotence: re-quarantining the same entry is a no-op
  - validation: missing/empty inputs return structured errors
  - reason defaults to "source_removed" matching the sweep taxonomy
  - other entries in the same pool are untouched
"""

from __future__ import annotations

import pytest

from work_buddy.triage.background import (
    STATE_PENDING,
    STATE_QUARANTINED,
    TriagePool,
    set_pool_for_tests,
)
from work_buddy.triage.capabilities.triage_pool_quarantine_entry import (
    triage_pool_quarantine_entry,
)
from work_buddy.triage.items import TriageItem


def _item(item_id: str) -> TriageItem:
    return TriageItem(
        id=item_id, text="t", label="L", source="email_message", metadata={},
    )


@pytest.fixture
def pool_with_entries(tmp_path):
    """A pool with three pending entries the test can quarantine selectively."""
    pool_dir = tmp_path / "triage_pool"
    pool_dir.mkdir()
    pool = TriagePool(pool_dir=pool_dir)
    set_pool_for_tests(pool)

    # Group by run_id so register_run sees all items for that run together.
    by_run: dict[str, list[TriageItem]] = {
        "bgt_alpha": [_item("email_a"), _item("email_b")],
        "bgt_beta":  [_item("email_c")],
    }
    for run_id, items in by_run.items():
        pool.register_run(
            run_id=run_id, adapter="email_triage", source="email_message",
            items=items,
        )
        for it in items:
            pool.submit_raw(run_id=run_id, item_id=it.id)

    yield pool
    set_pool_for_tests(None)


def _state_of(pool, run_id, item_id):
    """Read the persisted state of a single (run_id, item_id) entry."""
    index = pool._load_index()
    for raw in index.get("entries", []):
        if raw.get("run_id") == run_id and raw.get("item_id") == item_id:
            return raw.get("state")
    return None


def test_happy_path_quarantines_target(pool_with_entries):
    pool = pool_with_entries
    assert _state_of(pool, "bgt_alpha", "email_a") == STATE_PENDING

    out = triage_pool_quarantine_entry(run_id="bgt_alpha", item_id="email_a")
    assert out["ok"] is True
    assert out["stamped"] == 1
    assert out["reason"] == "source_removed"
    assert _state_of(pool, "bgt_alpha", "email_a") == STATE_QUARANTINED

    # Sibling entries are untouched
    assert _state_of(pool, "bgt_alpha", "email_b") == STATE_PENDING
    assert _state_of(pool, "bgt_beta", "email_c") == STATE_PENDING


def test_idempotent_repeat_quarantine(pool_with_entries):
    """Quarantining an already-quarantined entry returns stamped=0
    (mark_state's no-op-on-same-state contract)."""
    out1 = triage_pool_quarantine_entry(run_id="bgt_alpha", item_id="email_a")
    assert out1["stamped"] == 1
    out2 = triage_pool_quarantine_entry(run_id="bgt_alpha", item_id="email_a")
    assert out2["ok"] is True
    assert out2["stamped"] == 0


def test_unknown_entry_returns_zero_stamped(pool_with_entries):
    """Unknown (run_id, item_id) is not an error — just nothing changed."""
    out = triage_pool_quarantine_entry(run_id="bgt_nope", item_id="email_x")
    assert out["ok"] is True
    assert out["stamped"] == 0


def test_missing_run_id_returns_bad_request(pool_with_entries):
    out = triage_pool_quarantine_entry(run_id="", item_id="email_a")
    assert out["ok"] is False
    assert out["error_kind"] == "bad_request"


def test_missing_item_id_returns_bad_request(pool_with_entries):
    out = triage_pool_quarantine_entry(run_id="bgt_alpha", item_id="")
    assert out["ok"] is False
    assert out["error_kind"] == "bad_request"


def test_empty_reason_returns_bad_request(pool_with_entries):
    out = triage_pool_quarantine_entry(
        run_id="bgt_alpha", item_id="email_a", reason="   ",
    )
    assert out["ok"] is False
    assert out["error_kind"] == "bad_request"


def test_custom_reason_is_persisted(pool_with_entries):
    pool = pool_with_entries
    triage_pool_quarantine_entry(
        run_id="bgt_alpha", item_id="email_a",
        reason="user_dismissed_via_dashboard",
    )
    index = pool._load_index()
    raw = next(r for r in index["entries"]
               if r["run_id"] == "bgt_alpha" and r["item_id"] == "email_a")
    assert raw["state"] == STATE_QUARANTINED
    assert raw["quarantine_reason"] == "user_dismissed_via_dashboard"
