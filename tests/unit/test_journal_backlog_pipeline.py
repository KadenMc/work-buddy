"""End-to-end smoke tests for the journal-backlog pipeline.

Exercises the full chain (segment → manifest → cluster → route → rewrite)
with mocked LLM calls so the test is hermetic. The point is to verify
that the substrate-agnostic components compose cleanly with the new
line-range segmentation output.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from work_buddy.consent import grant_consent
from work_buddy.llm import LLMResponse, LLMRunner


@pytest.fixture(autouse=True)
def _grant_consents() -> None:
    grant_consent("journal_backlog_create_task", mode="always")
    grant_consent("journal_backlog_execute_routing", mode="always")
    grant_consent("journal.rewrite_running_notes", mode="always")


def _thread(tid: str, lines: list[int], raw_text: str) -> dict[str, Any]:
    return {"id": tid, "lines": lines, "raw_text": raw_text,
            "line_count": len(lines), "source_dates": [],
            "has_multi_flag": False}


def test_pipeline_segment_to_manifest_to_cluster(monkeypatch) -> None:
    """Substrate composition: line-range threads flow through the manifest
    builder into the clusterer and produce a non-empty review document."""
    from work_buddy.journal_backlog import (
        build_thread_manifest,
        generate_clustered_review,
    )

    threads = [
        _thread("t_0", [1], "- review tax forms"),
        _thread("t_1", [2], "- file taxes by April 15"),
        _thread("t_2", [3], "- check ETF allocations"),
    ]

    # Mock manifest builder to return controlled tags
    canned = iter([
        LLMResponse(content="", structured_output={
            "tags": ["tax-prep", "deadline"], "summary": "Review forms.",
        }),
        LLMResponse(content="", structured_output={
            "tags": ["tax-prep", "filing"], "summary": "File taxes.",
        }),
        LLMResponse(content="", structured_output={
            "tags": ["etf-tracking"], "summary": "Check ETFs.",
        }),
    ])
    monkeypatch.setattr(LLMRunner, "call", lambda self, **kw: next(canned))

    manifest = build_thread_manifest(threads)
    assert len(manifest) == 3

    review = generate_clustered_review(
        threads, manifest, journal_date="2026-04-24", source_dates=[],
    )
    assert "t_0" in review and "t_1" in review and "t_2" in review
    # Tax-prep entries cluster together; ETF stands alone.
    assert "tax-prep" in review or "etf-tracking" in review


def test_pipeline_route_then_rewrite_round_trip(tmp_path: Path) -> None:
    """Routing + rewrite: given a 3-thread plan with mixed actions, verify
    the rewritten Running Notes contains only the kept threads' content."""
    from work_buddy.journal_backlog import (
        build_rewrite_preview,
        execute_routing_plan,
    )

    # Set up a minimal vault for routing.
    (tmp_path / "tasks").mkdir()
    (tmp_path / "tasks" / "master-task-list.md").write_text(
        "# Master Task List\n\n", encoding="utf-8",
    )

    original_text = "review tax forms\nfile taxes\ncheck ETFs"
    threads = [
        _thread("t_0", [1], "review tax forms"),
        _thread("t_1", [2], "file taxes"),
        _thread("t_2", [3], "check ETFs"),
    ]

    # Plan: route t_0 to a task, delete t_1, skip t_2.
    plan = [
        {"id": "t_0", "action": "route", "destination_type": "task",
         "task_text": "Review tax forms"},
        {"id": "t_1", "action": "delete", "reason": "noise"},
        {"id": "t_2", "action": "skip"},
    ]
    from unittest.mock import patch
    from work_buddy.obsidian.tasks import mutations
    with patch.object(
        mutations, "create_task",
        return_value={"success": True, "task_line": "x", "task_id": "t-x",
                      "file": "tasks/master-task-list.md"},
    ):
        routing_result = execute_routing_plan(plan, vault_root=tmp_path)
    assert routing_result["success"] is True
    assert routing_result["summary"]["routed"] == 1
    assert routing_result["summary"]["deleted"] == 1
    assert routing_result["summary"]["skipped"] == 1

    # Rewrite: build the routing record (per-id action map) for the rewrite.
    routing_record = {"items": plan}
    preview = build_rewrite_preview(
        original_text=original_text,
        threads=threads,
        routing_record=routing_record,
    )
    out = preview["rewritten_text"]
    # Skipped thread's content remains; routed and deleted threads' content drops.
    assert "check ETFs" in out
    assert "review tax forms" not in out
    assert "file taxes" not in out
    # Removed and kept ids are correct.
    assert "t_2" in preview["kept_ids"]
    assert "t_0" in preview["removed_ids"]
    assert "t_1" in preview["removed_ids"]
