"""Compatibility and projection tests for the session activity ledger."""

from __future__ import annotations

import json
import time
from datetime import datetime


def _write_events(path, events: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )


def _mixed_events() -> list[dict]:
    return [
        {
            "ts": "2026-09-12T12:00:00+00:00",
            "type": "capability_invoked",
            "capability": "task_read",
            "category": "tasks",
            "status": "ok",
            "duration_ms": 4,
        },
        {
            "ts": "2026-09-12T12:01:00+00:00",
            "type": "skill_invoked",
            "skill": "task_read",
            "category": "tasks",
            "status": "ok",
            "duration_ms": 5,
        },
        {
            "ts": "2026-09-12T12:02:00+00:00",
            "type": "workflow_started",
            "workflow_name": "task-triage",
        },
    ]


def test_record_skill_writes_only_canonical_fields(tmp_path, monkeypatch):
    from work_buddy.mcp_server import activity_ledger

    ledger = tmp_path / "activity_ledger.jsonl"
    monkeypatch.setattr(activity_ledger, "_get_ledger_path", lambda _sid=None: ledger)

    activity_ledger.record_skill(
        "task_read",
        "tasks",
        "op_test",
        {"task_id": "t-1"},
        False,
        time.monotonic(),
        {"task_id": "t-1"},
        None,
        False,
        agent_session_id="session-test",
    )

    event = json.loads(ledger.read_text(encoding="utf-8"))
    assert event["type"] == "skill_invoked"
    assert event["skill"] == "task_read"
    assert "capability" not in event


def test_mixed_history_queries_and_summaries_use_skill_vocabulary(
    tmp_path, monkeypatch,
):
    from work_buddy.mcp_server import activity_ledger

    ledger = tmp_path / "activity_ledger.jsonl"
    _write_events(ledger, _mixed_events())
    monkeypatch.setattr(activity_ledger, "_get_ledger_path", lambda _sid=None: ledger)

    current = activity_ledger.query_activity(
        event_type="skill_invoked",
        skill_name="task_read",
    )
    deprecated = activity_ledger.query_activity(capability_name="task_read")

    assert current["filtered_count"] == 2
    assert deprecated["filtered_count"] == 2
    assert {event["type"] for event in current["events"]} == {
        "skill_invoked",
        "capability_invoked",
    }
    assert any("capability" in event for event in current["events"])

    summary = activity_ledger.query_session_summary()
    assert summary["skills_invoked"] == 2
    assert summary["by_skill"] == {"task_read": 2}
    assert "capabilities_invoked" not in summary
    assert "by_capability" not in summary


def test_canonical_activity_filter_wins_by_presence_over_deprecated_alias(
    tmp_path, monkeypatch,
):
    from work_buddy.mcp_server import activity_ledger

    ledger = tmp_path / "activity_ledger.jsonl"
    _write_events(ledger, _mixed_events())
    monkeypatch.setattr(activity_ledger, "_get_ledger_path", lambda _sid=None: ledger)

    result = activity_ledger.query_activity(
        skill_name="",
        capability_name="legacy_should_not_override",
    )

    assert result["filtered_count"] == 3


def test_context_bundle_formatter_reads_historical_invocations():
    from work_buddy.collectors.session_activity_collector import _format

    rendered = _format(
        {
            "session_id": "session-test",
            "total_events": 2,
            "duration_minutes": 1,
            "by_category": {"tasks": 2},
            "by_skill": {"task_read": 2},
        },
        {"events": _mixed_events()[:2]},
    )

    assert "Top skills invoked" in rendered
    assert rendered.count("`task_read`") == 3
    assert "Top capabilities" not in rendered


def test_session_activity_projection_normalizes_historical_events(monkeypatch):
    from work_buddy.mcp_server import activity_ledger
    from work_buddy.sessions.inspector import session_wb_activity

    monkeypatch.setattr(
        activity_ledger,
        "query_session_summary",
        lambda agent_session_id=None: {
            "session_id": agent_session_id,
            "total_events": 1,
            "skills_invoked": 1,
            "by_skill": {"task_read": 1},
        },
    )
    monkeypatch.setattr(
        activity_ledger,
        "query_activity",
        lambda **_kwargs: {"events": _mixed_events()[:1]},
    )

    result = session_wb_activity(session_id="session-test")

    assert result["skills_invoked"] == 1
    assert result["by_skill"] == {"task_read": 1}
    assert result["recent_events"] == [{
        "ts": "2026-09-12T12:00:00+00:00",
        "type": "skill_invoked",
        "skill": "task_read",
        "status": "ok",
    }]
    assert "by_capability" not in result


def test_activity_timeline_reads_both_invocation_event_shapes(monkeypatch):
    from work_buddy import activity
    from work_buddy.mcp_server import activity_ledger

    monkeypatch.setattr(
        activity_ledger,
        "query_activity",
        lambda **_kwargs: {"events": list(reversed(_mixed_events()[:2]))},
    )

    events = activity._collect_ledger_events(
        datetime(2026, 9, 12, 11, 59),
        datetime(2026, 9, 12, 12, 3),
    )

    assert len(events) == 2
    assert all("wb_run(task_read)" in event.summary for event in events)
