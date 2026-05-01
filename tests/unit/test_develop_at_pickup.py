"""Slice 7: develop-at-pickup propose + apply flow.

The LLM call is stubbed -- propose_decomposition with runner=None
returns the no-runner error path; we exercise apply_decomposition
directly to verify the safety rule + the density bump + the
set_current_to_first behavior.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from work_buddy.clarify import develop_at_pickup as dap
from work_buddy.obsidian.tasks import action_items, store


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "tasks.sqlite3"
    monkeypatch.setattr(store, "_db_path", lambda: db)
    yield db


# ---------------------------------------------------------------------------
# propose_decomposition -- no runner
# ---------------------------------------------------------------------------


def test_propose_no_runner_returns_error():
    out = dap.propose_decomposition("t-x")
    assert out.error and "runner" in out.error
    assert out.items == ()


def test_propose_unknown_task_returns_error(fresh_db):
    out = dap.propose_decomposition("t-missing", runner=MagicMock())
    assert out.error and "not found" in out.error


def test_propose_calls_call_for_verdict(fresh_db, monkeypatch):
    """Wire-up smoke test: confirm the call_for_verdict path is hit
    and the structured_output is parsed into items."""
    store.create(task_id="t-p", description="do thing")

    def fake_call_for_verdict(*, runner, tier, system, user, output_schema,
                              required_fields, caller, item_id):
        resp = MagicMock()
        resp.is_error.return_value = False
        resp.structured_output = {
            "rationale": "two-step decomposition",
            "items": [
                {"description": "step 1"},
                {"description": "step 2", "definition_of_done": "done!"},
            ],
            "confidence": 0.85,
        }
        resp.content = "raw"
        return resp

    monkeypatch.setattr(
        "work_buddy.clarify.verdict_call.call_for_verdict",
        fake_call_for_verdict,
    )
    out = dap.propose_decomposition("t-p", runner=MagicMock())
    assert out.error is None
    assert len(out.items) == 2
    assert out.items[0]["description"] == "step 1"
    assert out.items[1]["definition_of_done"] == "done!"
    assert out.confidence == pytest.approx(0.85)


def test_propose_drops_invalid_items(fresh_db, monkeypatch):
    """Items without a description are dropped."""
    store.create(task_id="t-p2")

    def fake(*, runner, tier, system, user, output_schema,
             required_fields, caller, item_id):
        resp = MagicMock()
        resp.is_error.return_value = False
        resp.structured_output = {
            "rationale": "...",
            "items": [
                {"description": "good"},
                {"description": ""},        # dropped
                {"foo": "bar"},             # dropped
            ],
        }
        resp.content = ""
        return resp

    monkeypatch.setattr(
        "work_buddy.clarify.verdict_call.call_for_verdict", fake,
    )
    out = dap.propose_decomposition("t-p2", runner=MagicMock())
    assert [i["description"] for i in out.items] == ["good"]


# ---------------------------------------------------------------------------
# apply_decomposition
# ---------------------------------------------------------------------------


def test_apply_persists_items_with_approval(fresh_db):
    store.create(task_id="t-a", density="sparse")
    res = dap.apply_decomposition(
        "t-a",
        approved_items=[
            {"description": "edit code"},
            {"description": "test code", "definition_of_done": "tests pass"},
        ],
    )
    assert res.items_created == 2
    items = action_items.list_for_task("t-a")
    assert [i["description"] for i in items] == ["edit code", "test code"]
    # Per ROADMAP §7 (PR #70 fix #2): agent-proposed user-approved
    # items land with authorship='agent_approved' (preserves origin).
    for i in items:
        assert i["authorship"] == "agent_approved"
    # is_executable admits these (approved).
    for i in items:
        assert action_items.is_executable(i) is True


def test_apply_sets_current_to_first(fresh_db):
    store.create(task_id="t-a2", density="sparse")
    res = dap.apply_decomposition(
        "t-a2",
        approved_items=[{"description": "one"}, {"description": "two"}],
    )
    assert res.set_current == res.item_ids[0]
    row = store.get("t-a2")
    assert row["current_action_item_id"] == res.set_current


def test_apply_can_skip_set_current(fresh_db):
    store.create(task_id="t-a3", density="sparse")
    res = dap.apply_decomposition(
        "t-a3",
        approved_items=[{"description": "x"}],
        set_current_to_first=False,
    )
    assert res.set_current is None
    row = store.get("t-a3")
    assert row["current_action_item_id"] is None


def test_apply_bumps_density_sparse_to_developed(fresh_db):
    store.create(task_id="t-a4", density="sparse")
    dap.apply_decomposition(
        "t-a4", approved_items=[{"description": "x"}],
    )
    assert store.get("t-a4")["density"] == "developed"


def test_apply_keeps_developed_density(fresh_db):
    store.create(task_id="t-a5", density="developed")
    dap.apply_decomposition(
        "t-a5", approved_items=[{"description": "x"}],
    )
    assert store.get("t-a5")["density"] == "developed"


def test_apply_refuses_empty_list(fresh_db):
    """Safety: empty approved_items would silently accept nothing.
    Refuse so the caller knows to handle the LLM's items=[] verdict
    differently (e.g. record as 'no decomposition needed')."""
    store.create(task_id="t-a6")
    res = dap.apply_decomposition("t-a6", approved_items=[])
    assert res.items_created == 0
    assert res.error and "empty" in res.error.lower()


def test_apply_drops_items_without_description(fresh_db):
    store.create(task_id="t-a7", density="sparse")
    res = dap.apply_decomposition(
        "t-a7",
        approved_items=[
            {"description": "good"},
            {"description": ""},
            {},
        ],
    )
    assert res.items_created == 1


def test_apply_serializes_contexts_and_risk(fresh_db):
    store.create(task_id="t-a8", density="sparse")
    res = dap.apply_decomposition(
        "t-a8",
        approved_items=[{
            "description": "send email",
            "agent_required_contexts": ["@email_send"],
            "user_required_contexts": ["@email_send", "@user_workstation"],
            "risk_profile": {
                "reversibility": "irreversible",
                "regret_potential": "high",
            },
        }],
    )
    item = action_items.get(res.item_ids[0])
    assert item["agent_required_contexts"] is not None
    assert "@email_send" in item["agent_required_contexts"]
    assert item["risk_profile_json"] is not None
    assert "irreversible" in item["risk_profile_json"]
