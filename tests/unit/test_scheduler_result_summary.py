"""Structured summaries for bounded sidecar capability results."""

from __future__ import annotations

from work_buddy.sidecar.scheduler.engine import _summarize_job_result


def test_bounded_index_result_unwraps_and_reports_partial_backlog():
    detail = {
        "result": {
            "partition": "conversation",
            "complete": False,
            "remaining": {
                "items": 75,
                "vectors": {"content": 3000, "aliases": 2},
            },
        }
    }

    assert _summarize_job_result("ok", detail) == (
        "ok: conversation partial; 75 items, 3002 vectors remaining"
    )


def test_bounded_index_result_reports_completion():
    detail = {
        "result": {
            "partition": "conversation",
            "complete": True,
            "remaining": {"items": 0, "vectors": {"content": 0}},
        }
    }

    assert _summarize_job_result("ok", detail) == (
        "ok: conversation complete; 0 items, 0 vectors remaining"
    )
