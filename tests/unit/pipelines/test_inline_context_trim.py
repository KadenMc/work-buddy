"""Tests for the inline-pipeline context-trimming helper.

The verdict's user-context block was previously dumping every active
project (with full descriptions) AND every active task (up to 12)
regardless of relevance to the captured thought. With the project-picker
SubCall now running before the verdict, the projects half is pure
double-context. The tasks half is wasteful — most of those tasks have
nothing to do with the one-line capture. ``_trim_context_for_verdict``
applies two principled trims:

1. Drop ``active_projects`` when the picker emitted candidates.
2. IR-filter ``active_tasks`` by relevance to ``captured_text + hint``,
   keep ``top_k``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from work_buddy.pipelines.inline import _trim_context_for_verdict


def _ctx(active_projects=None, active_tasks=None) -> dict[str, Any]:
    return {
        "active_tasks": active_tasks if active_tasks is not None else [],
        "active_contracts": [],
        "active_projects": active_projects if active_projects is not None else [],
        "recent_commits": [],
    }


# ---------------------------------------------------------------------------
# Active-projects drop
# ---------------------------------------------------------------------------


def test_drops_active_projects_when_picker_candidates_exist() -> None:
    """When the picker emitted any candidates, the verdict prompt
    should not also re-emit the full project registry."""
    in_ctx = _ctx(active_projects=[
        {"slug": "tka_paper", "name": "TKA", "description": "long..."},
        {"slug": "ifs", "name": "IFS", "description": "long..."},
    ])
    out = _trim_context_for_verdict(
        in_ctx, captured_text="text",
        has_picker_candidates=True,
    )
    assert out["active_projects"] == []


def test_keeps_active_projects_when_no_picker_candidates() -> None:
    """If the picker didn't run / soft-failed empty, the verdict needs
    the active_projects block as fallback context."""
    projects = [
        {"slug": "tka_paper", "name": "TKA", "description": "long..."},
    ]
    in_ctx = _ctx(active_projects=projects)
    out = _trim_context_for_verdict(
        in_ctx, captured_text="text",
        has_picker_candidates=False,
    )
    assert out["active_projects"] == projects


# ---------------------------------------------------------------------------
# Active-tasks IR filter
# ---------------------------------------------------------------------------


_TASKS = [
    {"task_id": "t-001", "state": "focused", "text": "Draft TKA paper intro"},
    {"task_id": "t-002", "state": "focused", "text": "Pay water bill"},
    {"task_id": "t-003", "state": "mit",     "text": "Email advisor about deadline"},
    {"task_id": "t-004", "state": "inbox",   "text": "Review IFS module rewrite"},
    {"task_id": "t-005", "state": "inbox",   "text": "Buy groceries"},
    {"task_id": "t-006", "state": "inbox",   "text": "Schedule dentist appointment"},
    {"task_id": "t-007", "state": "inbox",   "text": "Finalize ECG-FM revision plan"},
    {"task_id": "t-008", "state": "inbox",   "text": "Renew library books"},
]


def test_ir_filter_picks_top_k_in_score_order() -> None:
    """When hybrid_search returns scores, the trimmed task list reflects them."""
    fake_scored = [
        {"name": "t-001", "score": 0.92},
        {"name": "t-007", "score": 0.85},
        {"name": "t-003", "score": 0.71},
        {"name": "t-004", "score": 0.40},
        {"name": "t-005", "score": 0.10},
    ]
    in_ctx = _ctx(active_tasks=_TASKS)
    with patch("work_buddy.embedding.client.hybrid_search") as mock_search:
        mock_search.return_value = fake_scored
        out = _trim_context_for_verdict(
            in_ctx,
            captured_text="working on the TKA paper today",
            has_picker_candidates=False,
            task_top_k=3,
        )
    ids = [t["task_id"] for t in out["active_tasks"]]
    assert ids == ["t-001", "t-007", "t-003"]


def test_ir_filter_falls_back_unfiltered_when_service_down() -> None:
    """Embedding service unreachable: pass tasks through unchanged
    (no ranking, no truncation) so the verdict still has SOMETHING."""
    in_ctx = _ctx(active_tasks=_TASKS)
    with patch("work_buddy.embedding.client.hybrid_search") as mock_search:
        mock_search.side_effect = RuntimeError("service down")
        out = _trim_context_for_verdict(
            in_ctx,
            captured_text="some text",
            has_picker_candidates=False,
            task_top_k=3,
        )
    # Fallback: all 8 tasks preserved.
    assert len(out["active_tasks"]) == 8


def test_ir_filter_skipped_when_under_top_k() -> None:
    """If active_tasks already fits the cap, no IR call needed."""
    short_tasks = _TASKS[:3]
    in_ctx = _ctx(active_tasks=short_tasks)
    with patch("work_buddy.embedding.client.hybrid_search") as mock_search:
        out = _trim_context_for_verdict(
            in_ctx, captured_text="anything",
            has_picker_candidates=False,
            task_top_k=5,
        )
        mock_search.assert_not_called()
    assert out["active_tasks"] == short_tasks


def test_ir_query_includes_hint() -> None:
    """The IR query is built from captured_text + hint so a short capture
    with a clarifying hint can still reach relevant tasks."""
    in_ctx = _ctx(active_tasks=_TASKS)
    captured = []
    with patch("work_buddy.embedding.client.hybrid_search") as mock_search:
        mock_search.side_effect = lambda q, candidates, **kw: (
            captured.append(q) or [{"name": "t-001", "score": 0.9}]
        )
        _trim_context_for_verdict(
            in_ctx,
            captured_text="email advisor",
            hint="about TKA paper revisions",
            has_picker_candidates=False,
            task_top_k=2,
        )
    assert captured, "hybrid_search was never called"
    assert "TKA paper revisions" in captured[0]
    assert "email advisor" in captured[0]


def test_ir_filter_skipped_when_no_query() -> None:
    """Empty captured_text + empty hint: skip IR; pass through all tasks."""
    in_ctx = _ctx(active_tasks=_TASKS)
    with patch("work_buddy.embedding.client.hybrid_search") as mock_search:
        out = _trim_context_for_verdict(
            in_ctx, captured_text="", hint="",
            has_picker_candidates=False,
            task_top_k=3,
        )
        mock_search.assert_not_called()
    assert len(out["active_tasks"]) == 8


# ---------------------------------------------------------------------------
# Non-task / non-project fields are preserved verbatim
# ---------------------------------------------------------------------------


def test_other_context_sections_preserved_verbatim() -> None:
    """Contracts and recent_commits are passed through unchanged."""
    in_ctx = {
        "active_tasks": [],
        "active_contracts": [{"title": "X", "deadline": "2026-06-01", "claim": "a"}],
        "active_projects": [],
        "recent_commits": ["abc Subject"],
    }
    out = _trim_context_for_verdict(
        in_ctx, captured_text="t",
        has_picker_candidates=True,
    )
    assert out["active_contracts"] == in_ctx["active_contracts"]
    assert out["recent_commits"] == in_ctx["recent_commits"]
