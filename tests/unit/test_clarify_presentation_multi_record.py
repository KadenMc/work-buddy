"""Slice 3 tests: presentation builder for multi-record + refusal verdicts.

Covers ``_build_presentation_from_pool``'s Slice 3 branches:

- Multi-record verdicts surface ``records`` / ``refusal`` /
  ``is_multi_record=true`` on each presentation_group.
- Refusal-bearing verdicts get ``resolution_type=clarification``.
- Multi-record verdicts pick a representative legacy ``action`` key
  for groups_by_action so the existing frontend keeps working.
- ``likely_task_id`` surfaces from the first task record's
  ``target_task_id`` for multi-record entries.
- ``suggested_task_text`` falls back to the first task record's
  proposal text.
- Cluster-on-read defaults off (no ``clusters`` field on the
  presentation).
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from work_buddy.clarify.background import ClarifyPool
from work_buddy.clarify.capabilities.triage_review_pool import (
    _build_presentation_from_pool,
)
from work_buddy.clarify.items import TriageItem


def _isolated_pool() -> ClarifyPool:
    return ClarifyPool(pool_dir=pathlib.Path(tempfile.mkdtemp()))


def _register_run(pool: ClarifyPool, *, n_items: int = 5) -> None:
    items = [
        TriageItem(id=f"i{i}", text=f"text {i} content", label=f"l{i}", source="journal_thread")
        for i in range(n_items)
    ]
    pool.register_run(run_id="r1", adapter="test", source="journal_thread", items=items)


def _find_group_for_item(presentation: dict, item_id: str) -> dict | None:
    for groups in presentation["groups_by_action"].values():
        for g in groups:
            for it in g.get("items", []):
                if it.get("id") == item_id:
                    return g
    return None


def test_multi_record_task_lands_in_create_task_bucket() -> None:
    pool = _isolated_pool()
    _register_run(pool)
    pool.submit(run_id="r1", item_id="i0", verdict={
        "rationale": "r", "group_intent": "do thing",
        "records": [{
            "destination": "task",
            "task_proposal": {"suggested_task_text": "do the thing"},
        }],
    })
    pres = _build_presentation_from_pool(pool.pending())
    assert any(g["items"][0]["id"] == "i0" for g in pres["groups_by_action"]["create_task"])


def test_multi_record_all_delete_lands_in_close_bucket() -> None:
    pool = _isolated_pool()
    _register_run(pool)
    pool.submit(run_id="r1", item_id="i0", verdict={
        "rationale": "r", "group_intent": "drop",
        "records": [{"destination": "delete", "delete_reason": "duplicate"}],
    })
    pres = _build_presentation_from_pool(pool.pending())
    assert any(g["items"][0]["id"] == "i0" for g in pres["groups_by_action"]["close"])


def test_multi_record_empty_records_lands_in_leave_bucket() -> None:
    pool = _isolated_pool()
    _register_run(pool)
    pool.submit(run_id="r1", item_id="i0", verdict={
        "rationale": "r", "group_intent": "ambient", "records": [],
    })
    pres = _build_presentation_from_pool(pool.pending())
    assert any(g["items"][0]["id"] == "i0" for g in pres["groups_by_action"]["leave"])


def test_refusal_lands_in_leave_bucket_with_clarification_type() -> None:
    pool = _isolated_pool()
    _register_run(pool)
    pool.submit(run_id="r1", item_id="i0", verdict={
        "rationale": "r", "group_intent": "ambiguous",
        "refusal": {"question": "Which project?"},
    })
    pres = _build_presentation_from_pool(pool.pending())
    g = _find_group_for_item(pres, "i0")
    assert g is not None
    assert g["resolution_type"] == "clarification"
    assert g["refusal"]["question"] == "Which project?"


def test_multi_record_surfaces_records_and_is_multi_record_flag() -> None:
    pool = _isolated_pool()
    _register_run(pool)
    pool.submit(run_id="r1", item_id="i0", verdict={
        "rationale": "r", "group_intent": "test",
        "records": [
            {"destination": "task", "task_proposal": {"suggested_task_text": "t1"}},
            {"destination": "delete", "delete_reason": "dup"},
        ],
    })
    pres = _build_presentation_from_pool(pool.pending())
    g = _find_group_for_item(pres, "i0")
    assert g is not None
    assert g["is_multi_record"] is True
    assert len(g["records"]) == 2
    assert g["refusal"] is None


def test_multi_record_likely_task_id_from_target() -> None:
    """Multi-record entries surface the first task record's
    ``target_task_id`` as ``likely_task_id`` so the existing
    record-into-task UI keeps working."""
    pool = _isolated_pool()
    _register_run(pool)
    pool.submit(run_id="r1", item_id="i0", verdict={
        "rationale": "r", "group_intent": "test",
        "records": [{
            "destination": "task",
            "task_proposal": {
                "suggested_task_text": "merge into existing",
                "target_task_id": "t-existing-9",
            },
        }],
    })
    pres = _build_presentation_from_pool(pool.pending())
    g = _find_group_for_item(pres, "i0")
    assert g["likely_task_id"] == "t-existing-9"


def test_multi_record_suggested_task_text_from_first_task_record() -> None:
    pool = _isolated_pool()
    _register_run(pool)
    pool.submit(run_id="r1", item_id="i0", verdict={
        "rationale": "r", "group_intent": "test",
        "records": [
            {"destination": "delete", "delete_reason": "dup"},
            {"destination": "task", "task_proposal": {"suggested_task_text": "the task text"}},
        ],
    })
    pres = _build_presentation_from_pool(pool.pending())
    g = _find_group_for_item(pres, "i0")
    assert g.get("suggested_task_text") == "the task text"


def test_legacy_entry_is_multi_record_false() -> None:
    pool = _isolated_pool()
    _register_run(pool)
    pool.submit(run_id="r1", item_id="i0", verdict={
        "recommended_action": "create_task",
        "rationale": "r", "group_intent": "legacy",
        "suggested_task_text": "legacy thing",
    })
    pres = _build_presentation_from_pool(pool.pending())
    g = _find_group_for_item(pres, "i0")
    assert g is not None
    assert g["is_multi_record"] is False
    assert g["records"] is None
    assert g["resolution_type"] == "verdict_review"


def test_raw_entry_resolution_type() -> None:
    pool = _isolated_pool()
    _register_run(pool)
    pool.submit_raw(run_id="r1", item_id="i0")
    pres = _build_presentation_from_pool(pool.pending())
    g = _find_group_for_item(pres, "i0")
    assert g is not None
    assert g["is_raw"] is True
    assert g["resolution_type"] == "raw_capture"


def test_pipeline_blocker_persisted_through_presentation() -> None:
    pool = _isolated_pool()
    _register_run(pool)
    pool.submit(run_id="r1", item_id="i0", verdict={
        "rationale": "r", "group_intent": "blocked",
        "records": [{"destination": "task", "task_proposal": {"suggested_task_text": "t"}}],
        "pipeline_blocker": {"kind": "agent_context_unmet", "detail": "@email_send not configured"},
    })
    pres = _build_presentation_from_pool(pool.pending())
    g = _find_group_for_item(pres, "i0")
    assert g["pipeline_blocker"]["kind"] == "agent_context_unmet"
    assert g["pipeline_blocker"]["deep_link"] == "/setup"
    assert g["pipeline_blocker"]["detail"] == "@email_send not configured"


def test_clusters_field_absent_by_default() -> None:
    pool = _isolated_pool()
    _register_run(pool)
    for i in range(5):
        pool.submit(run_id="r1", item_id=f"i{i}", verdict={
            "recommended_action": "leave", "rationale": "r", "group_intent": "g",
        })
    pres = _build_presentation_from_pool(pool.pending())
    assert "clusters" not in pres


def test_pool_run_id_on_each_modal_item() -> None:
    """Slice 1.5 stamped pool_run_id on items so the Resolution
    Surface can target a specific entry. Slice 3 must preserve this."""
    pool = _isolated_pool()
    _register_run(pool)
    pool.submit(run_id="r1", item_id="i0", verdict={
        "rationale": "r", "group_intent": "g",
        "records": [{"destination": "task", "task_proposal": {"suggested_task_text": "t"}}],
    })
    pres = _build_presentation_from_pool(pool.pending())
    g = _find_group_for_item(pres, "i0")
    assert g["items"][0]["pool_run_id"] == "r1"
