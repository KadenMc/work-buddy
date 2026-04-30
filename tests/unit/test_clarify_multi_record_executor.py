"""Slice 3 tests: execute_triage_decisions on multi-record verdicts.

Covers the new ``_execute_multi_record_decisions`` path that fires
when a presentation_group carries ``records: [...]`` (the Slice 3
verdict shape):

- Per-record routing by destination (task / delete / reference /
  calendar_only).
- task records forward Slice 2 metadata to ``tasks_create`` (kind
  → task_kind plus all matching-name fields).
- record_into_task vs create_task fork on
  ``task_proposal.target_task_id``.
- delete / reference / calendar_only destinations log only (Slices
  6/10 wire actual execution).
- User-level overrides: ``action='leave'`` skips records;
  ``action='close'`` treats the whole group as a coarse delete.
- Results buckets carry per-record outcomes plus a group rollup.
"""

from __future__ import annotations

from typing import Any


def _make_multi_record_group(
    *,
    index: int = 0,
    records: list[dict[str, Any]] | None = None,
    intent: str = "test multi-record",
    items: list[dict[str, Any]] | None = None,
    suggested_action: str = "create_task",
) -> dict[str, Any]:
    """Build a presentation_group with the Slice 3 fields populated."""
    return {
        "index": index,
        "intent": intent,
        "rationale": "test rationale",
        "suggested_action": suggested_action,
        "items": items or [{"id": f"i{index}", "label": f"item {index}"}],
        "records": records or [],
        "is_multi_record": bool(records),
    }


def _make_presentation(groups: list[dict[str, Any]], source: str = "journal") -> dict[str, Any]:
    groups_by_action: dict[str, list] = {
        "close": [], "group": [], "create_task": [],
        "record_into_task": [], "leave": [],
    }
    for g in groups:
        groups_by_action[g.get("suggested_action", "leave")].append(g)
    return {
        "source": source,
        "groups_by_action": groups_by_action,
        "total_groups": len(groups),
        "total_items": sum(len(g.get("items", [])) for g in groups),
    }


def test_multi_record_task_destination_creates_task(monkeypatch) -> None:
    """A record with destination=task and a task_proposal should call
    tasks_create with Slice 2 metadata forwarded."""
    from work_buddy.clarify import execute

    captured: dict[str, Any] = {}

    def fake_create_task(**kw):
        captured.update(kw)
        return {"task_id": "t-multi-001", "task_text": kw.get("task_text", "")}

    monkeypatch.setattr(
        "work_buddy.obsidian.tasks.mutations.create_task", fake_create_task,
    )

    group = _make_multi_record_group(
        records=[{
            "destination": "task",
            "task_proposal": {
                "suggested_task_text": "redo Figure 3",
                "kind": "task",
                "outcome_text": "Figure 3 reflects new threshold",
                "creation_effort": "sparse",
                "user_involvement": "medium",
                "has_deadline": True,
                "deadline_date": "2026-05-15",
            },
        }],
    )
    pres = _make_presentation([group])
    decisions = {"group_decisions": [
        {"group_index": 0, "action": "create_task", "item_overrides": []},
    ]}
    summary = execute.execute_triage_decisions(decisions, pres)

    assert summary["tasks_created"] == 1
    assert summary["records_executed"] == 1
    assert summary["errors"] == 0
    # Slice 2 fields forwarded; ``kind`` mapped to ``task_kind``.
    assert captured["task_text"] == "redo Figure 3"
    assert captured["task_kind"] == "task"
    assert captured["outcome_text"] == "Figure 3 reflects new threshold"
    assert captured["creation_effort"] == "sparse"
    assert captured["user_involvement"] == "medium"
    assert captured["has_deadline"] is True
    assert captured["deadline_date"] == "2026-05-15"


def test_multi_record_record_into_task_fork(monkeypatch) -> None:
    """A task record with target_task_id forks into the
    record_into_task path (no tasks_create call)."""
    from work_buddy.clarify import execute

    create_calls = {"n": 0}

    def fake_create_task(**kw):
        create_calls["n"] += 1
        return {"task_id": "t-shouldnt-be-called"}

    monkeypatch.setattr(
        "work_buddy.obsidian.tasks.mutations.create_task", fake_create_task,
    )

    group = _make_multi_record_group(
        records=[{
            "destination": "task",
            "task_proposal": {
                "suggested_task_text": "ping co-author about caption",
                "target_task_id": "t-existing-1",
            },
        }],
    )
    pres = _make_presentation([group])
    decisions = {"group_decisions": [
        {"group_index": 0, "action": "create_task", "item_overrides": []},
    ]}
    summary = execute.execute_triage_decisions(decisions, pres)

    assert summary["tasks_created"] == 0
    assert summary["tasks_recorded"] == 1
    assert create_calls["n"] == 0
    rec = summary["details"]["tasks_recorded"][0]
    assert rec["target_task_id"] == "t-existing-1"
    assert rec["source_record_destination"] == "task"


def test_multi_record_delete_destination(monkeypatch) -> None:
    """delete records get logged in results.deleted with the reason."""
    from work_buddy.clarify import execute

    monkeypatch.setattr(
        "work_buddy.obsidian.tasks.mutations.create_task",
        lambda **kw: {"task_id": "t-x"},
    )

    group = _make_multi_record_group(
        records=[{
            "destination": "delete",
            "delete_reason": "duplicate of t-existing-1",
        }],
    )
    pres = _make_presentation([group])
    decisions = {"group_decisions": [
        {"group_index": 0, "action": "create_task", "item_overrides": []},
    ]}
    summary = execute.execute_triage_decisions(decisions, pres)

    assert summary["deleted"] == 1
    assert summary["tasks_created"] == 0
    assert summary["details"]["deleted"][0]["reason"] == "duplicate of t-existing-1"


def test_multi_record_reference_logs_only(monkeypatch) -> None:
    from work_buddy.clarify import execute

    monkeypatch.setattr(
        "work_buddy.obsidian.tasks.mutations.create_task",
        lambda **kw: {"task_id": "t-x"},
    )

    group = _make_multi_record_group(
        records=[{
            "destination": "reference",
            "reference_proposal": {
                "summary": "papers on ECG threshold methods",
                "suggested_path": "research/ecg/methods",
            },
        }],
    )
    pres = _make_presentation([group])
    decisions = {"group_decisions": [
        {"group_index": 0, "action": "create_task", "item_overrides": []},
    ]}
    summary = execute.execute_triage_decisions(decisions, pres)

    assert summary["references_logged"] == 1
    assert summary["tasks_created"] == 0
    logged = summary["details"]["references_logged"][0]
    assert logged["summary"] == "papers on ECG threshold methods"
    assert logged["suggested_path"] == "research/ecg/methods"


def test_multi_record_calendar_only_logs(monkeypatch) -> None:
    from work_buddy.clarify import execute

    monkeypatch.setattr(
        "work_buddy.obsidian.tasks.mutations.create_task",
        lambda **kw: {"task_id": "t-x"},
    )

    group = _make_multi_record_group(
        records=[{
            "destination": "calendar_only",
            "calendar_proposal": {
                "title": "team meeting",
                "datetime": "2026-05-10T15:00:00",
            },
        }],
    )
    pres = _make_presentation([group])
    decisions = {"group_decisions": [
        {"group_index": 0, "action": "create_task", "item_overrides": []},
    ]}
    summary = execute.execute_triage_decisions(decisions, pres)

    assert summary["calendar_logged"] == 1
    assert summary["details"]["calendar_logged"][0]["title"] == "team meeting"


def test_multi_record_birthday_plus_gift(monkeypatch) -> None:
    """One captured item produces two records (calendar + task);
    both should execute independently."""
    from work_buddy.clarify import execute

    create_calls: list[dict[str, Any]] = []

    def fake_create_task(**kw):
        create_calls.append(dict(kw))
        return {"task_id": f"t-fake-{len(create_calls):04d}"}

    monkeypatch.setattr(
        "work_buddy.obsidian.tasks.mutations.create_task", fake_create_task,
    )

    group = _make_multi_record_group(
        records=[
            {
                "destination": "calendar_only",
                "calendar_proposal": {
                    "title": "Sarah's 30th birthday",
                    "datetime": "2026-05-12T18:00:00",
                },
            },
            {
                "destination": "task",
                "task_proposal": {"suggested_task_text": "buy gift for Sarah"},
            },
        ],
        intent="Sarah's birthday",
    )
    pres = _make_presentation([group])
    decisions = {"group_decisions": [
        {"group_index": 0, "action": "create_task", "item_overrides": []},
    ]}
    summary = execute.execute_triage_decisions(decisions, pres)

    assert summary["tasks_created"] == 1
    assert summary["calendar_logged"] == 1
    assert create_calls[0]["task_text"] == "buy gift for Sarah"


def test_multi_record_user_override_leave_skips_records(monkeypatch) -> None:
    """User picking 'leave' on a multi-record group should skip every
    record and just mark the items as left."""
    from work_buddy.clarify import execute

    create_calls = {"n": 0}

    def fake_create_task(**kw):
        create_calls["n"] += 1
        return {"task_id": "t-x"}

    monkeypatch.setattr(
        "work_buddy.obsidian.tasks.mutations.create_task", fake_create_task,
    )

    group = _make_multi_record_group(
        records=[{
            "destination": "task",
            "task_proposal": {"suggested_task_text": "should not run"},
        }],
    )
    pres = _make_presentation([group])
    decisions = {"group_decisions": [
        {"group_index": 0, "action": "leave", "item_overrides": []},
    ]}
    summary = execute.execute_triage_decisions(decisions, pres)

    assert summary["tasks_created"] == 0
    assert create_calls["n"] == 0
    assert summary["left"] == 1


def test_multi_record_user_override_close_logs_delete(monkeypatch) -> None:
    """User picking 'close' on a multi-record group should record a
    coarse delete (records skipped, override flagged)."""
    from work_buddy.clarify import execute

    monkeypatch.setattr(
        "work_buddy.obsidian.tasks.mutations.create_task",
        lambda **kw: {"task_id": "t-x"},
    )

    group = _make_multi_record_group(
        records=[
            {"destination": "task", "task_proposal": {"suggested_task_text": "x"}},
            {"destination": "task", "task_proposal": {"suggested_task_text": "y"}},
        ],
    )
    pres = _make_presentation([group])
    decisions = {"group_decisions": [
        {"group_index": 0, "action": "close", "item_overrides": []},
    ]}
    summary = execute.execute_triage_decisions(decisions, pres)

    assert summary["tasks_created"] == 0
    assert summary["deleted"] == 1
    assert summary["details"]["deleted"][0]["reason"] == "user_overrode_to_close"
    assert summary["details"]["deleted"][0]["records_skipped"] == 2


def test_split_decisions_separates_legacy_and_multi_record() -> None:
    """The split helper routes multi-record groups to the new path
    and legacy groups to the existing _plan_operations path."""
    from work_buddy.clarify.execute import _split_decisions_by_shape

    group_mr = _make_multi_record_group(
        index=0,
        records=[{"destination": "task", "task_proposal": {"suggested_task_text": "t"}}],
    )
    group_legacy = {
        "index": 1,
        "intent": "legacy",
        "items": [{"id": "i1", "label": "l"}],
        "suggested_action": "create_task",
        "records": None,  # Legacy entries surface as None
        "is_multi_record": False,
    }
    pres = _make_presentation([group_mr, group_legacy])
    decisions = [
        {"group_index": 0, "action": "create_task"},
        {"group_index": 1, "action": "create_task"},
    ]
    multi, legacy = _split_decisions_by_shape(decisions, pres)
    assert len(multi) == 1
    assert len(legacy) == 1
    assert multi[0]["group_index"] == 0
    assert legacy[0]["group_index"] == 1


def test_empty_records_routes_through_legacy_leave(monkeypatch) -> None:
    """A multi-record group with empty records[] is treated as
    'leave' — falls through to the legacy path which logs items as
    left without making any LLM or vault calls."""
    from work_buddy.clarify import execute

    monkeypatch.setattr(
        "work_buddy.obsidian.tasks.mutations.create_task",
        lambda **kw: {"task_id": "t-x"},
    )

    group = _make_multi_record_group(records=[])
    pres = _make_presentation([group])
    decisions = {"group_decisions": [
        {"group_index": 0, "action": "leave", "item_overrides": []},
    ]}
    summary = execute.execute_triage_decisions(decisions, pres)

    assert summary["tasks_created"] == 0
    assert summary["left"] == 1
