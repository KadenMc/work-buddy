"""Slice 7: ``compute_pickup_readiness`` truth table.

Five-precedence ladder:

1. action items already exist → ready.
2. density='developed' or 'dense' → ready.
3. sparse + manual + medium/high involvement + has_deadline → ready.
4. sparse + agent-inferred + low involvement + stale → not ready.
5. otherwise → develop-first default.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from work_buddy.automation import pickup


_FROZEN_NOW = "2026-04-30T12:00:00+00:00"


def _days_ago(days: int) -> str:
    """Return an ISO timestamp ``days`` before _FROZEN_NOW."""
    now = datetime.fromisoformat(_FROZEN_NOW)
    earlier = now - timedelta(days=days)
    return earlier.isoformat()


# ---------------------------------------------------------------------------
# Rule 1: action items already exist
# ---------------------------------------------------------------------------


def test_existing_action_items_short_circuit_to_ready():
    decision = pickup.compute_pickup_readiness(
        task={"density": "sparse", "creation_effort": "sparse",
              "user_involvement": "low",
              "creation_provenance": "agent_inferred_from_journal"},
        world_state={"has_action_items": True},
        now_iso=_FROZEN_NOW,
    )
    assert decision.ready is True
    assert "action items" in decision.reason.lower()


# ---------------------------------------------------------------------------
# Rule 2: pre-developed density
# ---------------------------------------------------------------------------


def test_developed_task_is_ready():
    decision = pickup.compute_pickup_readiness(
        task={"density": "developed", "creation_effort": "developed"},
        now_iso=_FROZEN_NOW,
    )
    assert decision.ready is True
    assert "pre-developed" in decision.reason.lower()


def test_dense_task_is_ready():
    decision = pickup.compute_pickup_readiness(
        task={"density": "dense", "creation_effort": "developed"},
        now_iso=_FROZEN_NOW,
    )
    assert decision.ready is True


# ---------------------------------------------------------------------------
# Rule 3: sparse-but-intentional
# ---------------------------------------------------------------------------


def test_sparse_manual_with_deadline_is_ready():
    decision = pickup.compute_pickup_readiness(
        task={
            "density": "sparse",
            "creation_effort": "sparse",
            "user_involvement": "high",
            "creation_provenance": "manual",
            "has_deadline": True,
            "created_at": _days_ago(1),
        },
        now_iso=_FROZEN_NOW,
    )
    assert decision.ready is True
    assert "trust the user" in decision.reason.lower()


def test_sparse_manual_without_deadline_is_not_ready():
    """Manual + sparse + NO deadline → still develop-first default."""
    decision = pickup.compute_pickup_readiness(
        task={
            "density": "sparse",
            "creation_effort": "sparse",
            "user_involvement": "high",
            "creation_provenance": "manual",
            "has_deadline": False,
            "created_at": _days_ago(1),
        },
        now_iso=_FROZEN_NOW,
    )
    assert decision.ready is False


# ---------------------------------------------------------------------------
# Rule 4: stale agent-inferred → develop first
# ---------------------------------------------------------------------------


def test_stale_agent_inferred_low_involvement_is_not_ready():
    decision = pickup.compute_pickup_readiness(
        task={
            "density": "sparse",
            "creation_effort": "sparse",
            "user_involvement": "low",
            "creation_provenance": "agent_inferred_from_journal",
            "has_deadline": False,
            "created_at": _days_ago(pickup.SPARSE_STALENESS_DAYS + 2),
        },
        now_iso=_FROZEN_NOW,
    )
    assert decision.ready is False
    assert "aged" in decision.reason.lower()


def test_fresh_agent_inferred_low_involvement_falls_to_default():
    """Fresh agent-inferred → still develop-first, but reason is the default."""
    decision = pickup.compute_pickup_readiness(
        task={
            "density": "sparse",
            "creation_effort": "sparse",
            "user_involvement": "low",
            "creation_provenance": "agent_inferred_from_journal",
            "has_deadline": False,
            "created_at": _days_ago(1),
        },
        now_iso=_FROZEN_NOW,
    )
    assert decision.ready is False
    assert "develop-at-pickup default" in decision.reason


# ---------------------------------------------------------------------------
# Rule 5: develop-first default
# ---------------------------------------------------------------------------


def test_default_is_develop_first():
    decision = pickup.compute_pickup_readiness(
        task={"density": "sparse", "creation_effort": "medium",
              "user_involvement": "medium",
              "creation_provenance": "agent_inferred_from_chrome"},
        now_iso=_FROZEN_NOW,
    )
    assert decision.ready is False
    assert "develop-at-pickup default" in decision.reason


def test_empty_task_falls_to_default_develop_first():
    """Defensive: a None / empty task takes the safe path."""
    decision = pickup.compute_pickup_readiness(task=None)
    assert decision.ready is False


def test_signals_carries_per_decision_breakdown():
    decision = pickup.compute_pickup_readiness(
        task={"density": "developed"}, now_iso=_FROZEN_NOW,
    )
    assert decision.signals  # non-empty
    assert any("density=developed" in s for s in decision.signals)
