"""Unit tests for the ``/wb-task-completeness`` evidence gatherer.

Verifies :func:`work_buddy.task_completeness.gather_completeness_evidence`:
- composes read_task + per-session commits/writes/summary into one bundle
- degrades gracefully (status flips, errors recorded) when a sub-call raises
- treats a missing transcript (sidecar/pruned session) as a *note*, not a
  degradation — commit refresh is a freshening optimization, not required
- short-circuits cleanly when the task can't be read or has no sessions
"""

from __future__ import annotations

from unittest.mock import patch

from work_buddy import task_completeness as tc


def _ok_payload(sessions):
    return {"success": True, "text": "Fix the bug", "state": "inbox",
            "assigned_sessions": sessions}


class TestGatherEvidenceHappyPath:
    """A fully-populated task yields an ``ok`` bundle with per-session rows."""

    def test_bundle_shape(self):
        sessions = [{"session_id": "s-1", "assigned_at": "2026-05-01"}]
        with patch("work_buddy.obsidian.tasks.mutations.read_task",
                   return_value=_ok_payload(sessions)), \
             patch("work_buddy.conversation_observability.commits"
                   ".refresh_session_commits", return_value=None), \
             patch("work_buddy.conversation_observability.commits"
                   ".query_session_commits", return_value=[{"sha": "abc"}]), \
             patch("work_buddy.conversation_observability.writes"
                   ".query_session_writes", return_value=[{"path": "f.py"}]), \
             patch("work_buddy.conversation_observability.session_summary_row"
                   ".session_summary_row", return_value={"topic": "t"}):
            out = tc.gather_completeness_evidence("t-abc123")

        assert out["status"] == "ok"
        assert out["task_id"] == "t-abc123"
        assert out["assigned_sessions"] == sessions
        assert len(out["session_evidence"]) == 1
        entry = out["session_evidence"][0]
        assert entry["session_id"] == "s-1"
        assert entry["commits"] == [{"sha": "abc"}]
        assert entry["writes"] == [{"path": "f.py"}]
        assert entry["summary"] == {"topic": "t"}
        assert entry["note"] is None
        assert out["errors"] == []
        assert "now_iso" in out


class TestGatherEvidenceShortCircuits:
    """Task-level failures return early with status=error."""

    def test_read_task_raises(self):
        with patch("work_buddy.obsidian.tasks.mutations.read_task",
                   side_effect=RuntimeError("boom")):
            out = tc.gather_completeness_evidence("t-abc123")

        assert out["status"] == "error"
        assert out["task"] == {}
        assert any(e["step"] == "read_task" for e in out["errors"])
        assert "native" in out["cache_note"].lower()

    def test_task_not_found(self):
        with patch("work_buddy.obsidian.tasks.mutations.read_task",
                   return_value={"success": False, "message": "Task not found."}):
            out = tc.gather_completeness_evidence("t-missing")

        assert out["status"] == "error"
        assert out["cache_note"] == "Task not found."

    def test_no_sessions_assigned(self):
        with patch("work_buddy.obsidian.tasks.mutations.read_task",
                   return_value=_ok_payload([])):
            out = tc.gather_completeness_evidence("t-abc123")

        assert out["status"] == "ok"
        assert out["session_evidence"] == []
        assert "no session" in out["cache_note"].lower()


class TestGatherEvidenceDegradation:
    """Sub-call failures degrade gracefully instead of aborting."""

    def test_missing_transcript_is_a_note_not_degradation(self):
        """refresh raising 'no session found' → note, status stays ok."""
        sessions = [{"session_id": "sidecar-x", "assigned_at": "2026-05-01"}]
        with patch("work_buddy.obsidian.tasks.mutations.read_task",
                   return_value=_ok_payload(sessions)), \
             patch("work_buddy.conversation_observability.commits"
                   ".refresh_session_commits",
                   side_effect=RuntimeError("No session found")), \
             patch("work_buddy.conversation_observability.commits"
                   ".query_session_commits", return_value=[]), \
             patch("work_buddy.conversation_observability.writes"
                   ".query_session_writes", return_value=[]), \
             patch("work_buddy.conversation_observability.session_summary_row"
                   ".session_summary_row", return_value=None):
            out = tc.gather_completeness_evidence("t-abc123")

        assert out["status"] == "ok"
        entry = out["session_evidence"][0]
        assert entry["note"] is not None
        assert "transcript" in entry["note"].lower()
        assert out["errors"] == []

    def test_query_commits_failure_degrades(self):
        sessions = [{"session_id": "s-1", "assigned_at": "2026-05-01"}]
        with patch("work_buddy.obsidian.tasks.mutations.read_task",
                   return_value=_ok_payload(sessions)), \
             patch("work_buddy.conversation_observability.commits"
                   ".refresh_session_commits", return_value=None), \
             patch("work_buddy.conversation_observability.commits"
                   ".query_session_commits",
                   side_effect=RuntimeError("db locked")), \
             patch("work_buddy.conversation_observability.writes"
                   ".query_session_writes", return_value=[]), \
             patch("work_buddy.conversation_observability.session_summary_row"
                   ".session_summary_row", return_value=None):
            out = tc.gather_completeness_evidence("t-abc123")

        assert out["status"] == "degraded"
        assert any(e["step"].startswith("query_commits:") for e in out["errors"])

    def test_query_writes_failure_degrades(self):
        sessions = [{"session_id": "s-1", "assigned_at": "2026-05-01"}]
        with patch("work_buddy.obsidian.tasks.mutations.read_task",
                   return_value=_ok_payload(sessions)), \
             patch("work_buddy.conversation_observability.commits"
                   ".refresh_session_commits", return_value=None), \
             patch("work_buddy.conversation_observability.commits"
                   ".query_session_commits", return_value=[]), \
             patch("work_buddy.conversation_observability.writes"
                   ".query_session_writes",
                   side_effect=RuntimeError("db locked")), \
             patch("work_buddy.conversation_observability.session_summary_row"
                   ".session_summary_row", return_value=None):
            out = tc.gather_completeness_evidence("t-abc123")

        assert out["status"] == "degraded"
        assert any(e["step"].startswith("query_writes:") for e in out["errors"])
