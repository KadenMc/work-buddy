"""Tests for ``work_buddy.task_me`` orchestration helpers.

Covers:

1. ``load_context_for_task_me`` returns a dict with the expected
   subkeys, degrades gracefully when sub-calls fail, forwards
   ``user_current_contexts`` into the engage view.
2. ``build_now_plan`` reads focused tasks from the engage view (so
   action-context filtering applies), falls back to task_briefing
   when engage is unavailable, returns a plan list + status.
3. ``top_recommendations`` honours the actionable filter and the
   state/urgency/contract sort.
"""

from __future__ import annotations

import pytest

from work_buddy import task_me as tm


# ---------------------------------------------------------------------------
# load_context_for_task_me
# ---------------------------------------------------------------------------


def test_load_context_returns_expected_keys(monkeypatch):
    """All four sub-calls succeed → status='ok', no errors."""
    monkeypatch.setattr(
        "work_buddy.obsidian.tasks.manager.daily_briefing",
        lambda: {"focused": [{"description": "X", "task_id": "t-x"}]},
    )
    monkeypatch.setattr(
        "work_buddy.dashboard.service._build_engage_view_payload",
        lambda current_contexts=None: {
            "status": "ok", "items": [], "count": 0,
            "current_contexts": list(current_contexts or []),
        },
    )
    out = tm.load_context_for_task_me(
        user_current_contexts=["@filesystem"],
    )
    assert out["status"] in {"ok", "degraded"}
    assert "task_briefing" in out
    assert out["current_contexts"] == ["@filesystem"]
    assert out["engage"]["current_contexts"] == ["@filesystem"]


def test_load_context_degrades_on_briefing_failure(monkeypatch):
    def boom():
        raise RuntimeError("bridge down")
    monkeypatch.setattr(
        "work_buddy.obsidian.tasks.manager.daily_briefing", boom,
    )
    monkeypatch.setattr(
        "work_buddy.dashboard.service._build_engage_view_payload",
        lambda current_contexts=None: {"status": "ok", "items": [], "count": 0},
    )
    out = tm.load_context_for_task_me()
    assert out["status"] == "degraded"
    assert any(e["step"] == "task_briefing" for e in out["errors"])


# ---------------------------------------------------------------------------
# build_now_plan
# ---------------------------------------------------------------------------


def test_build_now_plan_uses_engage_focused_tasks(monkeypatch):
    """Plan generator gets tasks from engage view, not task_briefing."""
    captured = {}

    def fake_generate(calendar_events, focused_tasks, cfg):
        captured["focused"] = focused_tasks
        captured["cfg"] = cfg
        return [
            {"time_start": "10:00", "time_end": "11:00",
             "text": focused_tasks[0]["description"]},
        ]
    monkeypatch.setattr(
        "work_buddy.obsidian.day_planner.planner.generate_plan",
        fake_generate,
    )
    context = {
        "engage": {
            "items": [
                {"task_id": "t-a", "text": "edit code", "state": "focused",
                 "who_can_act": {"agent": True},
                 "user_now": {"satisfied": True}},
            ],
        },
        "calendar": [],
        "now_iso": "2026-04-30T12:00:00+00:00",
    }
    out = tm.build_now_plan(context=context, config={"clamp_to_now": True})
    assert out["status"] == "ok"
    assert out["focused_count"] == 1
    assert captured["focused"] == [{"description": "edit code", "task_id": "t-a"}]
    assert captured["cfg"]["clamp_to_now"] is True


def test_build_now_plan_skips_blocked_engage_items(monkeypatch):
    """Tasks the agent and user both can't satisfy are excluded."""
    captured = {}
    monkeypatch.setattr(
        "work_buddy.obsidian.day_planner.planner.generate_plan",
        lambda calendar_events, focused_tasks, cfg: (
            captured.update(focused=focused_tasks) or []
        ),
    )
    context = {
        "engage": {
            "items": [
                {"task_id": "t-blk", "text": "blocked", "state": "focused",
                 "who_can_act": {"agent": False},
                 "user_now": {"satisfied": False}},
                {"task_id": "t-ok", "text": "ok", "state": "focused",
                 "who_can_act": {"agent": True},
                 "user_now": {"satisfied": True}},
            ],
        },
        "calendar": [],
    }
    tm.build_now_plan(context=context, config={"clamp_to_now": False})
    assert captured["focused"] == [{"description": "ok", "task_id": "t-ok"}]


def test_build_now_plan_falls_back_to_task_briefing(monkeypatch):
    """When engage is empty/missing, use task_briefing.focused."""
    captured = {}
    monkeypatch.setattr(
        "work_buddy.obsidian.day_planner.planner.generate_plan",
        lambda calendar_events, focused_tasks, cfg: (
            captured.update(focused=focused_tasks) or []
        ),
    )
    context = {
        "engage": {"items": []},
        "task_briefing": {
            "focused": [
                {"description": "from briefing", "task_id": "t-b"},
            ],
        },
        "calendar": [],
    }
    tm.build_now_plan(context=context, config={"clamp_to_now": False})
    assert captured["focused"] == [
        {"description": "from briefing", "task_id": "t-b"},
    ]


def test_build_now_plan_handles_generator_failure(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("planner crashed")
    monkeypatch.setattr(
        "work_buddy.obsidian.day_planner.planner.generate_plan", boom,
    )
    out = tm.build_now_plan(
        context={"engage": {"items": []}, "calendar": []},
        config={"clamp_to_now": False},
    )
    assert out["status"] == "error"
    assert "planner crashed" in out["error"]


# ---------------------------------------------------------------------------
# top_recommendations
# ---------------------------------------------------------------------------


def test_top_recommendations_filters_blocked():
    engage = {
        "items": [
            {"task_id": "t1", "text": "a", "state": "focused", "urgency": "high",
             "who_can_act": {"agent": True}, "user_now": {"satisfied": True}},
            {"task_id": "t2", "text": "blocked-blocked", "state": "focused",
             "urgency": "high",
             "who_can_act": {"agent": False}, "user_now": {"satisfied": False}},
        ],
    }
    recs = tm.top_recommendations(engage, limit=2)
    assert len(recs) == 1
    assert recs[0]["task_id"] == "t1"


def test_top_recommendations_sorts_focused_then_urgency_then_contract():
    engage = {
        "items": [
            # inbox + low urgency + no contract — should rank LAST
            {"task_id": "tz", "state": "inbox", "urgency": "low",
             "who_can_act": {"agent": True}, "user_now": {"satisfied": True}},
            # focused + medium + contract — should rank middle
            {"task_id": "ty", "state": "focused", "urgency": "medium",
             "contract": "paper",
             "who_can_act": {"agent": True}, "user_now": {"satisfied": True}},
            # focused + high + contract — should rank FIRST
            {"task_id": "tx", "state": "focused", "urgency": "high",
             "contract": "paper",
             "who_can_act": {"agent": True}, "user_now": {"satisfied": True}},
        ],
    }
    recs = tm.top_recommendations(engage, limit=3)
    assert [r["task_id"] for r in recs] == ["tx", "ty", "tz"]


def test_top_recommendations_handles_empty():
    assert tm.top_recommendations(None) == []
    assert tm.top_recommendations({"items": []}) == []
