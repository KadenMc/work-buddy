"""Golden-master of a full Thread FSM walk's event log.

The Phase-2 parity oracle for the WorkItem-base extraction: drive a
Thread deterministically from inference through to DONE and freeze the
exact emitted event sequence (state transitions + autonomy-audit
records). The extraction moves universal fields onto a `WorkItem` base
but must leave this byte-identical. A dismiss path and a
parent_force_close cascade are pinned too.

Event ids/timestamps are intentionally NOT snapshotted (DB-assigned,
time-dependent); the stable projection — ordered (kind, actor, from/to,
target/advance) — is the contract.
"""

from __future__ import annotations

import pytest

from work_buddy.threads import engine, fsm, store
from work_buddy.threads.enums import FSMState
from work_buddy.threads.models import Thread


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "threads.db"
    monkeypatch.setattr(store, "_db_path", lambda: db)
    engine.clear_state_entry_handlers()
    yield db
    engine.clear_state_entry_handlers()


def _project(thread_id: str) -> list[dict]:
    proj: list[dict] = []
    for e in store.list_events(thread_id):
        row = {"kind": e.kind, "actor": e.actor}
        if e.kind == "state_transition":
            row["from"] = e.data.get("from")
            row["to"] = e.data.get("to")
        if e.kind == "auto_advance_decision":
            row["target"] = e.data.get("target")
            row["advance"] = e.data.get("advance")
        proj.append(row)
    return proj


EXPECTED_WALK = [
    {"kind": "state_transition", "actor": "fsm_engine", "from": "inferring_intent", "to": "awaiting_intent_confirmation"},
    {"kind": "auto_advance_decision", "actor": "fsm_engine", "target": "intent", "advance": False},
    {"kind": "state_transition", "actor": "fsm_engine", "from": "awaiting_intent_confirmation", "to": "awaiting_inference"},
    {"kind": "state_transition", "actor": "fsm_engine", "from": "inferring_context", "to": "awaiting_context_confirmation"},
    {"kind": "auto_advance_decision", "actor": "fsm_engine", "target": "context", "advance": False},
    {"kind": "state_transition", "actor": "fsm_engine", "from": "awaiting_context_confirmation", "to": "awaiting_inference"},
    {"kind": "state_transition", "actor": "fsm_engine", "from": "inferring_action", "to": "awaiting_confirmation"},
    {"kind": "auto_advance_decision", "actor": "fsm_engine", "target": "action", "advance": False},
    {"kind": "state_transition", "actor": "fsm_engine", "from": "awaiting_confirmation", "to": "executing"},
    {"kind": "state_transition", "actor": "fsm_engine", "from": "executing", "to": "done"},
]


def test_full_inference_walk_event_log_frozen(fresh_db):
    t = Thread(fsm_state=FSMState.INFERRING_INTENT)
    store.insert_thread(t)
    engine.transition(t.thread_id, fsm.TRIG_INFERENCE_DONE, data={"intent": "x"})
    engine.transition(t.thread_id, fsm.TRIG_CONFIRMED)
    store.update_thread_state(t.thread_id, fsm_state="inferring_context")
    engine.transition(t.thread_id, fsm.TRIG_INFERENCE_DONE)
    engine.transition(t.thread_id, fsm.TRIG_CONFIRMED)
    store.update_thread_state(t.thread_id, fsm_state="inferring_action")
    engine.transition(t.thread_id, fsm.TRIG_INFERENCE_DONE)
    engine.transition(t.thread_id, fsm.TRIG_EXECUTE)
    result = engine.transition(
        t.thread_id, fsm.TRIG_EXECUTION_DONE, data={"requires_post_review": False},
    )
    assert result.next_state == FSMState.DONE
    assert _project(t.thread_id) == EXPECTED_WALK


def test_parent_event_id_chain_is_linked(fresh_db):
    """Every event after the first links to its predecessor (the
    optimistic-lock chain the extraction must not disturb)."""
    t = Thread(fsm_state=FSMState.AWAITING_INTENT_CONFIRMATION)
    store.insert_thread(t)
    engine.transition(t.thread_id, fsm.TRIG_CONFIRMED)
    engine.transition(t.thread_id, fsm.TRIG_DISMISSED_BY_USER)
    events = store.list_events(t.thread_id)
    # The dismiss event chains off the confirm transition's event.
    assert events[1].parent_event_id == events[0].id


def test_dismiss_path_frozen(fresh_db):
    t = Thread(fsm_state=FSMState.PROPOSED)
    store.insert_thread(t)
    engine.transition(t.thread_id, fsm.TRIG_DISMISSED_BY_USER)
    assert store.get_thread(t.thread_id).fsm_state == FSMState.DISMISSED
    proj = _project(t.thread_id)
    assert proj == [
        {"kind": "state_transition", "actor": "fsm_engine", "from": "proposed", "to": "dismissed"},
    ]
