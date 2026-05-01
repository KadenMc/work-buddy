"""Slice 4: verify risk_profile flows from verdict → executor → create_task.

The Clarify multi-record verdict carries a ``task_proposal.risk_profile``
dict (per Slice 4 prompt addition).  The executor serializes that dict
via ``parse_risk_profile(...).to_json()`` and forwards it to
``create_task`` as ``risk_profile_json``.  When the verdict has no
risk_profile, no kwarg is set and ``create_task``'s safe-profile
fallback applies.

This complements ``test_clarify_multi_record_executor.py`` which covers
the rest of the Slice 2 metadata forwarding.
"""

from __future__ import annotations

import json
from typing import Any


def _make_group_with_proposal(proposal: dict[str, Any]) -> dict[str, Any]:
    return {
        "index": 0,
        "intent": "test",
        "rationale": "rationale",
        "suggested_action": "create_task",
        "items": [{"id": "i0", "label": "item"}],
        "records": [{"destination": "task", "task_proposal": proposal}],
        "is_multi_record": True,
    }


def _make_presentation(group: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": "journal",
        "groups_by_action": {
            "close": [], "group": [], "create_task": [group],
            "record_into_task": [], "leave": [],
        },
        "total_groups": 1,
        "total_items": 1,
    }


def test_risk_profile_dict_serializes_to_json_kwarg(monkeypatch) -> None:
    """A populated risk_profile in the verdict reaches create_task as JSON."""
    from work_buddy.clarify import execute

    captured: dict[str, Any] = {}

    def fake_create_task(**kw):
        captured.update(kw)
        return {"task_id": "t-rp-001"}

    monkeypatch.setattr(
        "work_buddy.obsidian.tasks.mutations.create_task", fake_create_task,
    )

    group = _make_group_with_proposal({
        "suggested_task_text": "send email to dean",
        "risk_profile": {
            "financial_cents": 0,
            "privacy": "public",
            "accuracy": "consequential",
            "compute": "instant",
            "reversibility": "irreversible",
            "regret_potential": "high",
            "inference_uncertainty": "medium",
        },
    })
    decisions = {"group_decisions": [
        {"group_index": 0, "action": "create_task", "item_overrides": []},
    ]}
    summary = execute.execute_triage_decisions(decisions, _make_presentation(group))

    assert summary["tasks_created"] == 1
    assert "risk_profile_json" in captured
    rp = json.loads(captured["risk_profile_json"])
    # All fields preserved (after clamp-to-safe ladder validation).
    assert rp["privacy"] == "public"
    assert rp["accuracy"] == "consequential"
    assert rp["reversibility"] == "irreversible"
    assert rp["regret_potential"] == "high"
    assert rp["inference_uncertainty"] == "medium"


def test_risk_profile_clamps_unknown_ladder_values(monkeypatch) -> None:
    """An LLM hallucinating off-ladder values shouldn't crash; clamp to safe."""
    from work_buddy.clarify import execute

    captured: dict[str, Any] = {}

    def fake_create_task(**kw):
        captured.update(kw)
        return {"task_id": "t-rp-002"}

    monkeypatch.setattr(
        "work_buddy.obsidian.tasks.mutations.create_task", fake_create_task,
    )

    group = _make_group_with_proposal({
        "suggested_task_text": "ambiguous",
        "risk_profile": {
            "privacy": "WAT",
            "accuracy": 12,
            "reversibility": "totally fine",
            "inference_uncertainty": None,
        },
    })
    decisions = {"group_decisions": [
        {"group_index": 0, "action": "create_task", "item_overrides": []},
    ]}
    execute.execute_triage_decisions(decisions, _make_presentation(group))

    rp = json.loads(captured["risk_profile_json"])
    assert rp["privacy"] == "none"
    assert rp["accuracy"] == "low_stakes"
    assert rp["reversibility"] == "trivial"
    # inference_uncertainty default is medium per ROADMAP §7 Q-i.
    assert rp["inference_uncertainty"] == "medium"


def test_no_risk_profile_means_no_kwarg(monkeypatch) -> None:
    """Verdict without risk_profile doesn't pass the kwarg → safe-profile fallback."""
    from work_buddy.clarify import execute

    captured: dict[str, Any] = {}

    def fake_create_task(**kw):
        captured.update(kw)
        return {"task_id": "t-rp-003"}

    monkeypatch.setattr(
        "work_buddy.obsidian.tasks.mutations.create_task", fake_create_task,
    )

    group = _make_group_with_proposal({"suggested_task_text": "no profile"})
    decisions = {"group_decisions": [
        {"group_index": 0, "action": "create_task", "item_overrides": []},
    ]}
    execute.execute_triage_decisions(decisions, _make_presentation(group))

    assert "risk_profile_json" not in captured
