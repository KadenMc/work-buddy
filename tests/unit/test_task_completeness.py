"""Unit tests for the ``/wb-task-completeness`` evidence gatherer.

Verifies :func:`work_buddy.task_completeness.gather_completeness_evidence`:
- folds the ``build_task_provenance`` block (created_by / assigned /
  developed_by) into the bundle and gathers per-session evidence over the
  UNION of all provenance roles — not just assigned sessions
- composes read_task + per-session commits/writes/summary into one bundle
- degrades gracefully (status flips, errors recorded) when a sub-call raises
- treats a missing transcript (sidecar/pruned session) as a *note*, not a
  degradation — commit refresh is a freshening optimization, not required
- short-circuits cleanly when the task can't be read; flags the Rung-3
  (no structural link) case when nothing relates to the task
"""

from __future__ import annotations

import contextlib
from unittest.mock import patch

from work_buddy import task_completeness as tc


def _ok_payload(sessions):
    return {"success": True, "text": "Fix the bug", "state": "inbox",
            "assigned_sessions": sessions}


def _prov(created_by=None, assigned=None, developed_by=None):
    """A build_task_provenance-shaped dict."""
    return {
        "task_id": "t-abc123",
        "created_by": created_by,
        "assigned": assigned or [],
        "developed_by": developed_by or [],
        "intent_attribution": {
            "computed": False, "hook": "wb-task-completeness", "reason": "r",
        },
    }


@contextlib.contextmanager
def _patched(payload, *, prov=None, commits=None, writes=None, summary=None,
             refresh_exc=None, commits_exc=None, writes_exc=None, prov_exc=None):
    """Patch every dependency the gatherer touches once a task reads OK."""
    def _rv(value, exc):
        return {"side_effect": exc} if exc else {"return_value": value}

    stack = contextlib.ExitStack()
    stack.enter_context(patch(
        "work_buddy.obsidian.tasks.mutations.read_task", return_value=payload))
    stack.enter_context(patch(
        "work_buddy.obsidian.tasks.provenance.build_task_provenance",
        **_rv(prov, prov_exc)))
    stack.enter_context(patch(
        "work_buddy.conversation_observability.commits.refresh_session_commits",
        **_rv(None, refresh_exc)))
    stack.enter_context(patch(
        "work_buddy.conversation_observability.commits.query_session_commits",
        **_rv(commits or [], commits_exc)))
    stack.enter_context(patch(
        "work_buddy.conversation_observability.writes.query_session_writes",
        **_rv(writes or [], writes_exc)))
    stack.enter_context(patch(
        "work_buddy.conversation_observability.session_summary_row"
        ".session_summary_row", return_value=summary))
    with stack:
        yield


class TestGatherEvidenceHappyPath:
    """A fully-populated task yields an ``ok`` bundle with per-session rows."""

    def test_bundle_shape(self):
        sessions = [{"session_id": "s-1", "assigned_at": "2026-05-01"}]
        prov = _prov(assigned=sessions)
        with _patched(_ok_payload(sessions), prov=prov,
                      commits=[{"sha": "abc"}], writes=[{"path": "f.py"}],
                      summary={"topic": "t"}):
            out = tc.gather_completeness_evidence("t-abc123")

        assert out["status"] == "ok"
        assert out["task_id"] == "t-abc123"
        assert out["assigned_sessions"] == sessions
        assert out["provenance"] == prov
        assert len(out["session_evidence"]) == 1
        entry = out["session_evidence"][0]
        assert entry["session_id"] == "s-1"
        assert entry["roles"] == ["assigned"]
        assert entry["commits"] == [{"sha": "abc"}]
        assert entry["writes"] == [{"path": "f.py"}]
        assert entry["summary"] == {"topic": "t"}
        assert entry["note"] is None
        assert out["errors"] == []
        assert "now_iso" in out


class TestGatherEvidenceProvenance:
    """The provenance block drives a union-of-roles evidence sweep."""

    def test_developed_but_unassigned_session_is_gathered(self):
        """A developed-by session with no assignment still gets an evidence
        row (roles=['developed']) — the archaeology we used to do by hand."""
        assigned = [{"session_id": "s-assigned", "assigned_at": "2026-05-01"}]
        prov = _prov(
            assigned=assigned,
            developed_by=[{"session_id": "s-dev", "rung": 2,
                           "awareness": "read_note", "classification": "informed"}],
        )
        with _patched(_ok_payload(assigned), prov=prov, commits=[{"sha": "x"}]):
            out = tc.gather_completeness_evidence("t-abc123")

        by_sid = {e["session_id"]: e for e in out["session_evidence"]}
        assert set(by_sid) == {"s-assigned", "s-dev"}
        assert by_sid["s-assigned"]["roles"] == ["assigned"]
        assert by_sid["s-dev"]["roles"] == ["developed"]

    def test_created_by_role_surfaced(self):
        prov = _prov(created_by="s-creator")
        with _patched(_ok_payload([]), prov=prov):
            out = tc.gather_completeness_evidence("t-abc123")

        by_sid = {e["session_id"]: e for e in out["session_evidence"]}
        assert by_sid["s-creator"]["roles"] == ["created"]

    def test_same_session_multiple_roles(self):
        sessions = [{"session_id": "s-1", "assigned_at": "2026-05-01"}]
        prov = _prov(
            created_by="s-1", assigned=sessions,
            developed_by=[{"session_id": "s-1", "rung": 1,
                           "awareness": "assigned", "classification": "informed"}],
        )
        with _patched(_ok_payload(sessions), prov=prov):
            out = tc.gather_completeness_evidence("t-abc123")

        assert len(out["session_evidence"]) == 1
        roles = out["session_evidence"][0]["roles"]
        assert set(roles) == {"created", "assigned", "developed"}

    def test_no_structural_link_flags_rung3(self):
        """No assignment, no developer, no creator → Rung-3 cache note."""
        with _patched(_ok_payload([]), prov=_prov()):
            out = tc.gather_completeness_evidence("t-abc123")

        assert out["status"] == "ok"
        assert out["session_evidence"] == []
        assert "rung-3" in out["cache_note"].lower()

    def test_provenance_build_failure_degrades_but_continues(self):
        """If build_task_provenance raises, degrade + provenance None, but
        assigned-session evidence is still gathered."""
        sessions = [{"session_id": "s-1", "assigned_at": "2026-05-01"}]
        with _patched(_ok_payload(sessions), prov_exc=RuntimeError("boom"),
                      commits=[{"sha": "abc"}]):
            out = tc.gather_completeness_evidence("t-abc123")

        assert out["status"] == "degraded"
        assert out["provenance"] is None
        assert any(e["step"] == "provenance" for e in out["errors"])
        # assigned session still produced an evidence row
        assert [e["session_id"] for e in out["session_evidence"]] == ["s-1"]


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


class TestGatherEvidenceDegradation:
    """Sub-call failures degrade gracefully instead of aborting."""

    def test_missing_transcript_is_a_note_not_degradation(self):
        """refresh raising 'no session found' → note, status stays ok."""
        sessions = [{"session_id": "sidecar-x", "assigned_at": "2026-05-01"}]
        with _patched(_ok_payload(sessions), prov=_prov(assigned=sessions),
                      refresh_exc=RuntimeError("No session found")):
            out = tc.gather_completeness_evidence("t-abc123")

        assert out["status"] == "ok"
        entry = out["session_evidence"][0]
        assert entry["note"] is not None
        assert "transcript" in entry["note"].lower()
        assert out["errors"] == []

    def test_query_commits_failure_degrades(self):
        sessions = [{"session_id": "s-1", "assigned_at": "2026-05-01"}]
        with _patched(_ok_payload(sessions), prov=_prov(assigned=sessions),
                      commits_exc=RuntimeError("db locked")):
            out = tc.gather_completeness_evidence("t-abc123")

        assert out["status"] == "degraded"
        assert any(e["step"].startswith("query_commits:") for e in out["errors"])

    def test_query_writes_failure_degrades(self):
        sessions = [{"session_id": "s-1", "assigned_at": "2026-05-01"}]
        with _patched(_ok_payload(sessions), prov=_prov(assigned=sessions),
                      writes_exc=RuntimeError("db locked")):
            out = tc.gather_completeness_evidence("t-abc123")

        assert out["status"] == "degraded"
        assert any(e["step"].startswith("query_writes:") for e in out["errors"])
