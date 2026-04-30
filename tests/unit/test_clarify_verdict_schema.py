"""Slice 3 schema + submit-kwarg routing tests.

Covers:
- ``MULTI_RECORD_VERDICT_SCHEMA`` requires rationale + group_intent;
  records / refusal are both optional but the discriminator picks
  refusal-bearing verdicts as ``clarification`` resolution-type.
- ``is_multi_record_verdict`` correctly identifies the new shape.
- ``verdict_to_submit_kwargs`` filters BOTH legacy and multi-record
  fields without losing any required key.
- The pool-layer validation (``ClarifyPool.submit``) accepts both
  shapes and rejects:
    * neither shape present
    * unknown destination string
    * malformed records list
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from work_buddy.clarify.background import ClarifyPool
from work_buddy.clarify.items import (
    TRIAGE_ACTIONS, TRIAGE_DESTINATIONS, TriageItem,
)
from work_buddy.clarify.resolution import (
    RESOLUTION_TYPE_CLARIFICATION,
    RESOLUTION_TYPE_RAW_CAPTURE,
    RESOLUTION_TYPE_VERDICT_REVIEW,
    derive_resolution_type,
)
from work_buddy.clarify.verdict_schema import (
    MULTI_RECORD_VERDICT_SCHEMA,
    VERDICT_SCHEMA,
    is_multi_record_verdict,
    verdict_to_submit_kwargs,
)


# ---------------------------------------------------------------------------
# Schema declaration assertions
# ---------------------------------------------------------------------------


def test_legacy_schema_required_fields() -> None:
    assert "recommended_action" in VERDICT_SCHEMA["required"]
    assert "rationale" in VERDICT_SCHEMA["required"]
    assert set(VERDICT_SCHEMA["properties"]["recommended_action"]["enum"]) == set(TRIAGE_ACTIONS)


def test_multi_record_schema_required_fields() -> None:
    assert "rationale" in MULTI_RECORD_VERDICT_SCHEMA["required"]
    assert "group_intent" in MULTI_RECORD_VERDICT_SCHEMA["required"]
    # records + refusal are NOT required at the JSON-schema level.
    assert "records" not in MULTI_RECORD_VERDICT_SCHEMA["required"]
    assert "refusal" not in MULTI_RECORD_VERDICT_SCHEMA["required"]


def test_multi_record_schema_destination_enum_matches_constants() -> None:
    record_props = MULTI_RECORD_VERDICT_SCHEMA["properties"]["records"]["items"]["properties"]
    assert set(record_props["destination"]["enum"]) == set(TRIAGE_DESTINATIONS)


def test_task_proposal_schema_carries_slice_2_fields() -> None:
    record_props = MULTI_RECORD_VERDICT_SCHEMA["properties"]["records"]["items"]["properties"]
    task_proposal = record_props["task_proposal"]["properties"]
    for field in (
        "kind", "outcome_text", "next_action_text", "definition_of_done",
        "creation_effort", "user_involvement", "creation_provenance",
        "has_deadline", "deadline_date", "has_dependency", "dependency_hint",
        "suggested_task_text", "target_task_id",
    ):
        assert field in task_proposal, f"task_proposal missing {field!r}"


def test_task_proposal_forward_compat_fields_optional() -> None:
    record_props = MULTI_RECORD_VERDICT_SCHEMA["properties"]["records"]["items"]["properties"]
    task_proposal = record_props["task_proposal"]
    required = task_proposal["required"]
    assert required == ["suggested_task_text"]
    # Tier / risk_profile / required_contexts are optional — Slices 4/5b
    # populate them later.
    props = task_proposal["properties"]
    for field in ("tier", "risk_profile", "required_contexts"):
        assert field in props
        assert field not in required


# ---------------------------------------------------------------------------
# Discriminators
# ---------------------------------------------------------------------------


def test_is_multi_record_verdict_records_list() -> None:
    assert is_multi_record_verdict({"records": []}) is True
    assert is_multi_record_verdict({"records": [{"destination": "task"}]}) is True


def test_is_multi_record_verdict_refusal() -> None:
    assert is_multi_record_verdict({"refusal": {"question": "?"}}) is True


def test_is_multi_record_verdict_legacy() -> None:
    assert is_multi_record_verdict({"recommended_action": "leave"}) is False


def test_is_multi_record_verdict_raw() -> None:
    assert is_multi_record_verdict({"raw": True}) is False


def test_is_multi_record_verdict_garbage() -> None:
    assert is_multi_record_verdict(None) is False  # type: ignore[arg-type]
    assert is_multi_record_verdict("not a dict") is False  # type: ignore[arg-type]


def test_derive_resolution_type_priority() -> None:
    """Refusal first, raw second, verdict_review default."""
    assert derive_resolution_type({"refusal": {"question": "?"}}) == RESOLUTION_TYPE_CLARIFICATION
    # Refusal overrides raw.
    assert derive_resolution_type(
        {"refusal": {"question": "?"}, "raw": True}
    ) == RESOLUTION_TYPE_CLARIFICATION
    assert derive_resolution_type({"raw": True}) == RESOLUTION_TYPE_RAW_CAPTURE
    assert derive_resolution_type({"recommended_action": "leave"}) == RESOLUTION_TYPE_VERDICT_REVIEW
    assert derive_resolution_type({"records": []}) == RESOLUTION_TYPE_VERDICT_REVIEW


# ---------------------------------------------------------------------------
# verdict_to_submit_kwargs filtering
# ---------------------------------------------------------------------------


def test_verdict_to_submit_kwargs_legacy_shape() -> None:
    verdict = {
        "recommended_action": "create_task",
        "rationale": "looks like a task",
        "group_intent": "legacy task",
        "confidence": 0.9,
        "suggested_task_text": "do thing",
        "made_up_field": "should be filtered",
    }
    out = verdict_to_submit_kwargs(verdict)
    assert "recommended_action" in out
    assert "rationale" in out
    assert "group_intent" in out
    assert "confidence" in out
    assert "suggested_task_text" in out
    assert "made_up_field" not in out


def test_verdict_to_submit_kwargs_multi_record_shape() -> None:
    verdict = {
        "rationale": "r",
        "group_intent": "g",
        "confidence": 0.8,
        "records": [{"destination": "task", "task_proposal": {"suggested_task_text": "t"}}],
        "pipeline_blocker": {"kind": "consent_required"},
    }
    out = verdict_to_submit_kwargs(verdict)
    assert out["records"] == verdict["records"]
    assert out["pipeline_blocker"] == verdict["pipeline_blocker"]
    assert "recommended_action" not in out


def test_verdict_to_submit_kwargs_refusal_shape() -> None:
    verdict = {
        "rationale": "r",
        "group_intent": "g",
        "refusal": {"question": "which project?", "missing_context": ["project"]},
    }
    out = verdict_to_submit_kwargs(verdict)
    assert out["refusal"] == verdict["refusal"]
    assert "records" not in out


def test_verdict_to_submit_kwargs_drops_none_values() -> None:
    out = verdict_to_submit_kwargs({"rationale": "r", "confidence": None})
    assert "confidence" not in out


# ---------------------------------------------------------------------------
# Pool submit validation (Slice 3 dual-shape acceptance)
# ---------------------------------------------------------------------------


def _isolated_pool() -> ClarifyPool:
    return ClarifyPool(pool_dir=pathlib.Path(tempfile.mkdtemp()))


def _register_run(pool: ClarifyPool, *, run_id: str = "r1", n_items: int = 5) -> None:
    items = [
        TriageItem(id=f"i{i}", text=f"text {i}", label=f"l{i}", source="journal_thread")
        for i in range(n_items)
    ]
    pool.register_run(run_id=run_id, adapter="test", source="journal_thread", items=items)


def test_pool_submit_legacy_shape_ok() -> None:
    pool = _isolated_pool()
    _register_run(pool)
    r = pool.submit(run_id="r1", item_id="i0", verdict={
        "recommended_action": "create_task",
        "rationale": "r",
    })
    assert r["status"] == "ok"


def test_pool_submit_multi_record_task_ok() -> None:
    pool = _isolated_pool()
    _register_run(pool)
    r = pool.submit(run_id="r1", item_id="i0", verdict={
        "rationale": "r", "group_intent": "g",
        "records": [{
            "destination": "task",
            "task_proposal": {"suggested_task_text": "t"},
        }],
    })
    assert r["status"] == "ok"


def test_pool_submit_empty_records_ok() -> None:
    pool = _isolated_pool()
    _register_run(pool)
    r = pool.submit(run_id="r1", item_id="i0", verdict={
        "rationale": "r", "group_intent": "g", "records": [],
    })
    assert r["status"] == "ok"


def test_pool_submit_refusal_ok() -> None:
    pool = _isolated_pool()
    _register_run(pool)
    r = pool.submit(run_id="r1", item_id="i0", verdict={
        "rationale": "r", "group_intent": "g",
        "refusal": {"question": "which project?"},
    })
    assert r["status"] == "ok"


def test_pool_submit_neither_shape_rejected() -> None:
    pool = _isolated_pool()
    _register_run(pool)
    r = pool.submit(run_id="r1", item_id="i0", verdict={
        "rationale": "r", "group_intent": "g",
    })
    assert r["status"] == "error"
    assert "either" in r["error"].lower() or "must include" in r["error"].lower()


def test_pool_submit_unknown_destination_rejected() -> None:
    pool = _isolated_pool()
    _register_run(pool)
    r = pool.submit(run_id="r1", item_id="i0", verdict={
        "rationale": "r", "group_intent": "g",
        "records": [{"destination": "made_up"}],
    })
    assert r["status"] == "error"
    assert "made_up" in r["error"]


def test_pool_submit_records_with_non_dict_rejected() -> None:
    pool = _isolated_pool()
    _register_run(pool)
    r = pool.submit(run_id="r1", item_id="i0", verdict={
        "rationale": "r", "group_intent": "g",
        "records": ["not a dict"],
    })
    assert r["status"] == "error"


def test_pool_submit_legacy_unknown_action_rejected() -> None:
    pool = _isolated_pool()
    _register_run(pool)
    r = pool.submit(run_id="r1", item_id="i0", verdict={
        "recommended_action": "made_up",
        "rationale": "r",
    })
    assert r["status"] == "error"


def test_pool_submit_persists_multi_record_fields() -> None:
    """All Slice 3 fields survive the round-trip through _shape_verdict."""
    pool = _isolated_pool()
    _register_run(pool)
    pool.submit(run_id="r1", item_id="i0", verdict={
        "rationale": "r", "group_intent": "g", "confidence": 0.7,
        "records": [{"destination": "task", "task_proposal": {"suggested_task_text": "t"}}],
        "pipeline_blocker": {"kind": "consent_required", "detail": "oh no"},
    })
    pe = pool.all_entries()[0]
    assert pe.verdict["rationale"] == "r"
    assert pe.verdict["group_intent"] == "g"
    assert pe.verdict["confidence"] == 0.7
    assert pe.verdict["records"][0]["destination"] == "task"
    assert pe.verdict["pipeline_blocker"]["kind"] == "consent_required"


def test_pool_submit_persists_refusal() -> None:
    pool = _isolated_pool()
    _register_run(pool)
    pool.submit(run_id="r1", item_id="i0", verdict={
        "rationale": "r", "group_intent": "g",
        "refusal": {"question": "?", "missing_context": ["project"]},
    })
    pe = pool.all_entries()[0]
    assert pe.verdict["refusal"]["question"] == "?"
    assert pe.verdict["refusal"]["missing_context"] == ["project"]


# ---------------------------------------------------------------------------
# Backwards-compat aliases (Slice 3 rename)
# ---------------------------------------------------------------------------


def test_legacy_class_aliases_work() -> None:
    """``TriagePool`` and ``PoolEntry`` aliases preserve external imports."""
    from work_buddy.clarify.background import (
        ClarifyEntry, ClarifyPool, PoolEntry, TriagePool,
    )
    assert TriagePool is ClarifyPool
    assert PoolEntry is ClarifyEntry


def test_triage_shim_module_available() -> None:
    """``import work_buddy.triage.background`` should resolve to the
    same module as ``work_buddy.clarify.background``."""
    import work_buddy.clarify.background as new_path
    import work_buddy.triage.background as legacy_path
    assert legacy_path.ClarifyPool is new_path.ClarifyPool
    assert legacy_path.TriagePool is new_path.ClarifyPool
