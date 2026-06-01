"""Characterization: NULL / empty ``risk_profile`` handling.

`risk_profile` is one of the universal fields Phase 2 moves onto the
`WorkItem` base. In production 0/79 open tasks carry a populated risk
profile, so the NULL path is the *common* case and must be pinned before
the field changes homes. The existing thread suite seeds populated
profiles, so this path was previously unexercised (noted in
`automation/risk.py`).
"""

from __future__ import annotations

import pytest

from work_buddy.threads import engine, store
from work_buddy.threads.enums import FSMState
from work_buddy.threads.models import Thread


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "threads.db"
    monkeypatch.setattr(store, "_db_path", lambda: db)
    engine.clear_state_entry_handlers()
    yield db
    engine.clear_state_entry_handlers()


def test_default_thread_has_empty_risk_profile(fresh_db):
    t = Thread()
    store.insert_thread(t)
    fetched = store.get_thread(t.thread_id)
    assert fetched.risk_profile == {}


def test_from_row_none_risk_profile_json_becomes_empty_dict():
    row = {
        "thread_id": "th-legacy01",
        "fsm_state": "proposed",
        "autonomy_policy_json": None,
        "context_items_json": None,
        "risk_profile_json": None,
        "inciting_event_summary_json": None,
    }
    t = Thread.from_row(row)
    assert t.risk_profile == {}
    assert t.inciting_event_summary == {}
    assert t.context_items == ()


def test_from_row_empty_json_string_becomes_empty_dict():
    row = {
        "thread_id": "th-legacy02",
        "fsm_state": "proposed",
        "risk_profile_json": "{}",
    }
    t = Thread.from_row(row)
    assert t.risk_profile == {}


def test_populated_risk_profile_round_trips(fresh_db):
    profile = {"reversibility": "irreversible", "regret_potential": "high"}
    t = Thread(risk_profile=profile)
    store.insert_thread(t)
    fetched = store.get_thread(t.thread_id)
    assert fetched.risk_profile == profile
    # to_dict surfaces it under the universal key (guards the Phase-2
    # to_dict split — risk_profile must survive on the base side).
    assert fetched.to_dict()["risk_profile"] == profile


def test_to_dict_from_row_round_trip_preserves_all_keys(fresh_db):
    """Full round-trip with a populated, non-default profile +
    inciting summary — the 18-key to_dict contract the extraction's
    _universal_dict() split must preserve."""
    t = Thread(
        fsm_state=FSMState.AWAITING_CONFIRMATION,
        risk_profile={"accuracy": "critical"},
        inciting_event_summary={"source": "journal", "key": "abc"},
    )
    store.insert_thread(t)
    fetched = store.get_thread(t.thread_id)
    d = fetched.to_dict()
    # Re-hydrating the serialized form yields an equal dict.
    again = Thread.from_row({
        "thread_id": d["thread_id"],
        "parent_id": d["parent_id"],
        "subtype": d["subtype"],
        "fsm_state": d["fsm_state"],
        "parent_event_id": d["parent_event_id"],
        "autonomy_policy_json": d["autonomy_policy"],
        "context_items_json": d["context_items"],
        "risk_profile_json": d["risk_profile"],
        "inciting_event_summary_json": d["inciting_event_summary"],
        "created_at": d["created_at"],
        "updated_at": d["updated_at"],
        "archived_at": d["archived_at"],
        "current_focus_thread_id": d["current_focus_thread_id"],
        "resurface_at": d["resurface_at"],
        "order_index": d["order_index"],
        "search_blob": d["search_blob"],
        "parent_relationship": d["parent_relationship"],
        "originating_scrape_id": d["originating_scrape_id"],
    })
    assert again.to_dict() == d
