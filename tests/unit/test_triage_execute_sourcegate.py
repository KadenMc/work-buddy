"""Phase 1 tests: execute_triage_decisions behaves correctly when
the presentation's source is journal (not chrome).

Covers:
- Chrome-only bootstrap (``_get_current_tabs``) is skipped for
  non-chrome sources — the Chrome collector is never imported.
- Stray ``close``/``group`` ops under a non-chrome source are
  recorded as errors, not silently dispatched.
- ``create_task`` + ``record_into_task`` flow through and use the
  source-specific note headers.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest


def _make_presentation(
    source: str,
    groups: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a minimal presentation dict for the executor to chew on."""
    groups_by_action: dict[str, list] = {
        "close": [], "group": [], "create_task": [],
        "record_into_task": [], "leave": [],
    }
    for g in groups or []:
        groups_by_action[g.get("suggested_action", "leave")].append(g)
    return {
        "source": source,
        "groups_by_action": groups_by_action,
        "total_groups": len(groups or []),
        "total_items": sum(len(g.get("items", [])) for g in (groups or [])),
    }


def _make_decision(
    group_index: int,
    action: str,
    *,
    new_task_text: str | None = None,
    target_task_id: str | None = None,
    item_ids: list[str] | None = None,
) -> dict[str, Any]:
    d: dict[str, Any] = {
        "group_index": group_index,
        "action": action,
        "item_ids": item_ids or [],
        "item_overrides": [],
    }
    if new_task_text:
        d["new_task_text"] = new_task_text
    if target_task_id:
        d["target_task_id"] = target_task_id
    return d


def test_journal_source_skips_chrome_collector(monkeypatch) -> None:
    """Non-chrome source must not even attempt to import / call the
    Chrome tabs collector — it's a hard module that may fail."""
    from work_buddy.triage import execute

    called = {"n": 0}

    def boom() -> dict:
        called["n"] += 1
        raise RuntimeError("chrome collector must not be called for journal")

    monkeypatch.setattr(execute, "_get_current_tabs", boom)

    presentation = _make_presentation(
        source="journal",
        groups=[{
            "index": 0,
            "suggested_action": "leave",
            "items": [{"id": "journal_t_aaaaaa", "label": "note"}],
        }],
    )
    decisions = {"group_decisions": [
        _make_decision(0, "leave", item_ids=["journal_t_aaaaaa"]),
    ]}
    summary = execute.execute_triage_decisions(decisions, presentation)
    assert called["n"] == 0
    assert summary["errors"] == 0
    assert summary["left"] == 1


def test_journal_source_stray_close_becomes_error() -> None:
    """A 'close' decision under a journal presentation must not get
    dispatched to the Chrome closer — it should be recorded as an
    error so the caller knows something's off."""
    from work_buddy.triage import execute

    presentation = _make_presentation(
        source="journal",
        groups=[{
            "index": 0,
            "suggested_action": "close",
            "items": [{"id": "journal_t_bbbbbb", "label": "orphan"}],
        }],
    )
    decisions = {"group_decisions": [
        _make_decision(0, "close", item_ids=["journal_t_bbbbbb"]),
    ]}
    summary = execute.execute_triage_decisions(decisions, presentation)
    assert summary["closed"] == 0
    assert summary["errors"] == 1
    err = summary["details"]["errors"][0]
    assert "Chrome-only" in err["error"]


def test_journal_create_task_uses_journal_header(monkeypatch) -> None:
    """Journal source must produce a 'Source Notes' header, not
    'Source Tabs'."""
    from work_buddy.triage import execute

    captured: dict[str, Any] = {}

    def fake_create_task(task_text: str, urgency: str, summary: str | None = None):
        captured["task_text"] = task_text
        captured["summary"] = summary or ""
        return {"task_id": "t-fake-01"}

    monkeypatch.setattr(
        "work_buddy.obsidian.tasks.mutations.create_task", fake_create_task,
    )

    presentation = _make_presentation(
        source="journal",
        groups=[{
            "index": 0,
            "suggested_action": "create_task",
            "items": [{"id": "journal_t_cccccc", "label": "ETF check"}],
            "suggested_task_text": "Check ETFs weekly",
        }],
    )
    decisions = {"group_decisions": [
        _make_decision(
            0, "create_task",
            new_task_text="Check ETFs weekly",
            item_ids=["journal_t_cccccc"],
        ),
    ]}
    summary = execute.execute_triage_decisions(decisions, presentation)
    assert summary["tasks_created"] == 1
    assert summary["errors"] == 0
    assert captured["task_text"] == "Check ETFs weekly"
    assert "## Source Notes" in captured["summary"]
    assert "## Source Tabs" not in captured["summary"]
    # Journal items have no URL, so the bullet is just the label.
    assert "ETF check" in captured["summary"]


def test_chrome_create_task_still_says_source_tabs(monkeypatch) -> None:
    """Regression check: Chrome keeps its original 'Source Tabs'
    header so existing Chrome triage runs are unaffected."""
    from work_buddy.triage import execute

    captured: dict[str, Any] = {}

    def fake_create_task(task_text: str, urgency: str, summary: str | None = None):
        captured["summary"] = summary or ""
        return {"task_id": "t-fake-02"}

    monkeypatch.setattr(
        "work_buddy.obsidian.tasks.mutations.create_task", fake_create_task,
    )
    # Chrome path calls _get_current_tabs → stub it so tests don't
    # need a live Chrome collector.
    monkeypatch.setattr(execute, "_get_current_tabs", lambda: {})

    presentation = _make_presentation(
        source="chrome",
        groups=[{
            "index": 0,
            "suggested_action": "create_task",
            "items": [{"id": "tab_1", "label": "Example", "url": "https://e.co/"}],
            "suggested_task_text": "Follow up on example",
        }],
    )
    decisions = {"group_decisions": [
        _make_decision(
            0, "create_task",
            new_task_text="Follow up on example",
            item_ids=["tab_1"],
        ),
    ]}
    summary = execute.execute_triage_decisions(decisions, presentation)
    assert summary["tasks_created"] == 1
    assert "## Source Tabs" in captured["summary"]
    assert "https://e.co/" in captured["summary"]


def test_unknown_source_falls_back_to_generic_header(monkeypatch) -> None:
    from work_buddy.triage import execute

    captured: dict[str, Any] = {}

    def fake_create_task(task_text: str, urgency: str, summary: str | None = None):
        captured["summary"] = summary or ""
        return {"task_id": "t-fake-03"}

    monkeypatch.setattr(
        "work_buddy.obsidian.tasks.mutations.create_task", fake_create_task,
    )

    presentation = _make_presentation(
        source="some_future_source",
        groups=[{
            "index": 0,
            "suggested_action": "create_task",
            "items": [{"id": "x_1", "label": "thing"}],
            "suggested_task_text": "Do the thing",
        }],
    )
    decisions = {"group_decisions": [
        _make_decision(
            0, "create_task",
            new_task_text="Do the thing",
            item_ids=["x_1"],
        ),
    ]}
    execute.execute_triage_decisions(decisions, presentation)
    assert "## Source Items" in captured["summary"]
