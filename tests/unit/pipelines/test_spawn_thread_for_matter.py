"""Tests for the per-matter spawn primitive in pipelines/singular.py.

The primitive orchestrates the per-matter pipeline (deadline +
project picker + verdict + spawn) for any singular-input source.
Used by ``inline_capture`` once per detected matter (1 if the
segmenter returns one segment, N for multi-matter captures).

These tests stub the LLM-bound pre-passes and the verdict so the
spawn-shape decision logic + thread-creation glue is exercised
without real LLM calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from work_buddy.llm.response import LLMResponse
from work_buddy.pipelines.singular import (
    ThreadSpawnResult,
    spawn_thread_for_matter,
)
from work_buddy.threads import store
from work_buddy.threads.enums import FSMState


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Per-test threads DB."""
    db = tmp_path / "threads.db"
    monkeypatch.setattr(store, "_db_path", lambda: db)
    yield


def _patch_pre_passes():
    """Stub deadline_extract + project_picker so they don't hit the LLM."""
    deadline_patch = patch(
        "work_buddy.clarify.deadline_extract.extract_deadline_hints",
        return_value={
            "has_deadline": False,
            "deadline_date": None,
            "has_dependency": False,
            "dependency_hint": None,
        },
    )
    picker_patch = patch(
        "work_buddy.clarify.project_picker.pick_projects",
        return_value={"candidates": [
            {"project_tag": None, "confidence": 1.0, "rationale": "stub"},
        ]},
    )
    return deadline_patch, picker_patch


def _patch_verdict(verdict_dict: dict | None, error: str | None = None):
    """Stub the multi-record verdict call."""
    return patch(
        "work_buddy.pipelines.inline._call_multi_record_verdict",
        return_value=(verdict_dict, error),
    )


def _patch_runner_constructor():
    """LLMRunner() called inside the verdict; mocked away."""
    runner = MagicMock()
    runner.call.return_value = LLMResponse(
        structured_output={"records": []},
        model="claude-sonnet-test",
    )
    return patch("work_buddy.llm.LLMRunner", MagicMock(return_value=runner))


# ---------------------------------------------------------------------------
# kind="error" — verdict couldn't run
# ---------------------------------------------------------------------------


def test_verdict_error_returns_error_kind(fresh_db):
    deadline_p, picker_p = _patch_pre_passes()
    with deadline_p, picker_p, _patch_verdict(None, error="empty tier_chain"):
        result = spawn_thread_for_matter(
            matter_text="some captured text long enough to exceed bypass",
            item_id="inline_test01_m0",
            source="inline",
        )
    assert result.kind == "error"
    assert result.thread_id is None
    assert result.error == "empty tier_chain"


# ---------------------------------------------------------------------------
# kind="refusal"
# ---------------------------------------------------------------------------


def test_refusal_verdict_spawns_one_clarification_thread(fresh_db):
    deadline_p, picker_p = _patch_pre_passes()
    refusal_verdict = {
        "rationale": "Need more context to commit.",
        "group_intent": "Ambiguous capture",
        "refusal": {
            "question": "Which project does this belong to?",
            "missing_context": ["project"],
        },
    }
    with deadline_p, picker_p, _patch_verdict(refusal_verdict):
        result = spawn_thread_for_matter(
            matter_text="Maybe figure 3 needs adjusting?",
            item_id="inline_test02_m0",
            source="inline",
        )
    assert result.kind == "refusal"
    assert result.thread_id is not None
    spawned = store.get_thread(result.thread_id)
    assert spawned is not None
    # No children for refusal-only path.
    children = store.list_threads(parent_id=result.thread_id)
    assert children == []


# ---------------------------------------------------------------------------
# kind="dismissed"
# ---------------------------------------------------------------------------


def test_all_delete_records_spawns_dismissed_thread(fresh_db):
    deadline_p, picker_p = _patch_pre_passes()
    all_delete_verdict = {
        "rationale": "Stray test ping; safe to drop.",
        "group_intent": "Test ping",
        "records": [
            {"destination": "delete", "delete_reason": "test ping"},
        ],
    }
    with deadline_p, picker_p, _patch_verdict(all_delete_verdict):
        result = spawn_thread_for_matter(
            matter_text="testing 1 2 3",
            item_id="inline_test03_m0",
            source="inline",
        )
    assert result.kind == "dismissed"
    assert result.thread_id is not None
    assert result.dropped_count == 1
    spawned = store.get_thread(result.thread_id)
    assert spawned.fsm_state == FSMState.DISMISSED


# ---------------------------------------------------------------------------
# kind="flat" — single actionable record, no umbrella
# ---------------------------------------------------------------------------


def test_single_record_spawns_flat_thread(fresh_db):
    deadline_p, picker_p = _patch_pre_passes()
    one_record_verdict = {
        "rationale": "User wants to email Bob.",
        "group_intent": "Email Bob",
        "records": [
            {
                "destination": "task",
                "task_proposal": {
                    "suggested_task_text": "Email Bob about the report",
                    "user_involvement": "high",
                    "creation_provenance": "inline-inferred",
                },
            },
        ],
    }
    with deadline_p, picker_p, _patch_verdict(one_record_verdict):
        result = spawn_thread_for_matter(
            matter_text="Email Bob about the report",
            item_id="inline_test04_m0",
            source="inline",
        )
    assert result.kind == "flat"
    assert result.thread_id is not None
    assert result.child_thread_ids == ()
    spawned = store.get_thread(result.thread_id)
    assert spawned.parent_id is None  # flat = root
    assert spawned.fsm_state == FSMState.AWAITING_CONFIRMATION


# ---------------------------------------------------------------------------
# kind="singular_umbrella" — 2+ actionable records
# ---------------------------------------------------------------------------


def test_two_records_spawn_singular_umbrella(fresh_db):
    deadline_p, picker_p = _patch_pre_passes()
    two_record_verdict = {
        "rationale": "One matter (birthday) → task + calendar event.",
        "group_intent": "Sarah's birthday gift",
        "records": [
            {
                "destination": "task",
                "task_proposal": {
                    "suggested_task_text": "Buy gift for Sarah",
                    "user_involvement": "high",
                    "creation_provenance": "inline-inferred",
                },
            },
            {
                "destination": "calendar_only",
                "calendar_proposal": {
                    "title": "Sarah's birthday",
                    "datetime": "2026-05-12",
                    "all_day": True,
                },
            },
        ],
    }
    with deadline_p, picker_p, _patch_verdict(two_record_verdict):
        result = spawn_thread_for_matter(
            matter_text="Buy gift for Sarah's birthday on May 12",
            item_id="inline_test05_m0",
            source="inline",
        )
    assert result.kind == "singular_umbrella"
    assert result.thread_id is not None
    assert len(result.child_thread_ids) == 2

    umbrella = store.get_thread(result.thread_id)
    assert umbrella.fsm_state == FSMState.MONITORING
    # Stage 1 marker: this umbrella is recognisably "singular"
    # (drives the dashboard render hoist).
    assert umbrella.parent_relationship == "singular"

    for cid in result.child_thread_ids:
        child = store.get_thread(cid)
        assert child.parent_id == result.thread_id
        assert child.fsm_state == FSMState.AWAITING_CONFIRMATION


# ---------------------------------------------------------------------------
# Audit fields populated on the result
# ---------------------------------------------------------------------------


def test_result_carries_deadline_and_project_audit_fields(fresh_db):
    deadline_p, picker_p = _patch_pre_passes()
    one_record_verdict = {
        "rationale": "Email Bob.",
        "group_intent": "Email Bob",
        "records": [
            {
                "destination": "task",
                "task_proposal": {"suggested_task_text": "Email Bob"},
            },
        ],
    }
    with deadline_p, picker_p, _patch_verdict(one_record_verdict):
        result = spawn_thread_for_matter(
            matter_text="Email Bob about the report",
            item_id="inline_test06_m0",
            source="inline",
        )
    assert result.deadline_hints is not None
    assert isinstance(result.project_candidates, list)
    assert result.verdict == one_record_verdict


# ---------------------------------------------------------------------------
# Multi-matter scenario — caller invokes the primitive N times
# ---------------------------------------------------------------------------


def test_multi_matter_caller_pattern_n_independent_threads(fresh_db):
    """When inline_capture detects N matters, it calls
    spawn_thread_for_matter N times. Each call produces its own root
    thread (no shared umbrella between matters)."""
    deadline_p, picker_p = _patch_pre_passes()
    # Two different verdicts for two different matters.
    matter_a_verdict = {
        "rationale": "Matter A.",
        "group_intent": "Email Bob",
        "records": [{
            "destination": "task",
            "task_proposal": {"suggested_task_text": "Email Bob"},
        }],
    }
    matter_b_verdict = {
        "rationale": "Matter B.",
        "group_intent": "Renew car insurance",
        "records": [{
            "destination": "task",
            "task_proposal": {"suggested_task_text": "Renew car insurance"},
        }],
    }
    verdicts = [matter_a_verdict, matter_b_verdict]

    def fake_verdict_call(**_kw):
        return verdicts.pop(0), None

    with deadline_p, picker_p, patch(
        "work_buddy.pipelines.inline._call_multi_record_verdict",
        side_effect=fake_verdict_call,
    ):
        r1 = spawn_thread_for_matter(
            matter_text="Email Bob about the report",
            item_id="inline_test07_m0",
            source="inline",
        )
        r2 = spawn_thread_for_matter(
            matter_text="Renew car insurance Friday",
            item_id="inline_test07_m1",
            source="inline",
        )

    # Each spawn produced a flat root thread, no umbrella conflation.
    assert r1.kind == "flat"
    assert r2.kind == "flat"
    assert r1.thread_id != r2.thread_id

    t1 = store.get_thread(r1.thread_id)
    t2 = store.get_thread(r2.thread_id)
    assert t1.parent_id is None
    assert t2.parent_id is None
    assert t1.fsm_state == FSMState.AWAITING_CONFIRMATION
    assert t2.fsm_state == FSMState.AWAITING_CONFIRMATION
