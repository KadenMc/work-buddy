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
             refresh_exc=None, commits_exc=None, writes_exc=None, prov_exc=None,
             note_readers=None, note_uuid="note-1", readers_exc=None,
             commits_by_sid=None, writes_by_sid=None):
    """Patch every dependency the gatherer touches once a task reads OK.

    ``note_readers`` stubs ``sessions_who_read_task`` (default: none) and
    ``note_uuid`` stubs the store row the gatherer reads to drive it — so
    tests never touch the real ~/.claude/projects tree or task DB.

    ``commits_by_sid`` / ``writes_by_sid`` (dicts keyed by session_id) give
    per-session control — needed to test the note-reader prune, where one
    session has work and another doesn't. When unset, the flat
    ``commits`` / ``writes`` return-value applies to every session.
    """
    def _rv(value, exc):
        return {"side_effect": exc} if exc else {"return_value": value}

    def _by_sid(mapping, flat, exc):
        if exc:
            return {"side_effect": exc}
        if mapping is not None:
            return {"side_effect": lambda session_id=None, **kw: mapping.get(session_id, [])}
        return {"return_value": flat or []}

    stack = contextlib.ExitStack()
    stack.enter_context(patch(
        "work_buddy.obsidian.tasks.mutations.read_task", return_value=payload))
    stack.enter_context(patch(
        "work_buddy.obsidian.tasks.provenance.build_task_provenance",
        **_rv(prov, prov_exc)))
    stack.enter_context(patch(
        "work_buddy.obsidian.tasks.store.get",
        return_value={"note_uuid": note_uuid}))
    stack.enter_context(patch(
        "work_buddy.obsidian.tasks.provenance.sessions_who_read_task",
        **_rv(note_readers or [], readers_exc)))
    stack.enter_context(patch(
        "work_buddy.conversation_observability.commits.refresh_session_commits",
        **_rv(None, refresh_exc)))
    stack.enter_context(patch(
        "work_buddy.conversation_observability.commits.query_session_commits",
        **_by_sid(commits_by_sid, commits, commits_exc)))
    stack.enter_context(patch(
        "work_buddy.conversation_observability.writes.query_session_writes",
        **_by_sid(writes_by_sid, writes, writes_exc)))
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


class TestGatherEvidenceNoteReaders:
    """The note_reader role surfaces Rung-3 'read it, forgot to toggle' devs."""

    def _reader(self, sid):
        return {"session_id": sid, "awareness": "read_note",
                "sources": {"read_tool": {"first": None, "last": None, "count": 1}},
                "first_seen": None, "last_seen": None}

    def test_note_reader_with_commit_surfaced(self):
        """A reader that ALSO committed (unrelated to the task id) is the
        strongest Rung-3 candidate — surfaced with the note_reader role."""
        with _patched(
            _ok_payload([]), prov=_prov(),
            note_readers=[self._reader("s-reader")],
            commits_by_sid={"s-reader": [{"sha": "deadbee"}]},
        ):
            out = tc.gather_completeness_evidence("t-abc123")

        by_sid = {e["session_id"]: e for e in out["session_evidence"]}
        assert "s-reader" in by_sid
        assert by_sid["s-reader"]["roles"] == ["note_reader"]
        assert by_sid["s-reader"]["commits"] == [{"sha": "deadbee"}]

    def test_pure_triage_read_pruned(self):
        """A reader with no commits and no writes is a triage sweep — pruned."""
        with _patched(
            _ok_payload([]), prov=_prov(),
            note_readers=[self._reader("s-triage")],
            commits_by_sid={}, writes_by_sid={},
        ):
            out = tc.gather_completeness_evidence("t-abc123")

        assert [e["session_id"] for e in out["session_evidence"]] == []

    def test_note_reader_with_writes_survives(self):
        with _patched(
            _ok_payload([]), prov=_prov(),
            note_readers=[self._reader("s-writer")],
            commits_by_sid={}, writes_by_sid={"s-writer": [{"path": "x.py"}]},
        ):
            out = tc.gather_completeness_evidence("t-abc123")

        by_sid = {e["session_id"]: e for e in out["session_evidence"]}
        assert by_sid["s-writer"]["writes"] == [{"path": "x.py"}]

    def test_reader_merges_with_developed_role(self):
        """A session that is BOTH developed-by and a note-reader keeps both
        roles (de-duped)."""
        prov = _prov(developed_by=[{"session_id": "s-1", "rung": 2,
                                    "awareness": "read_note",
                                    "classification": "informed"}])
        with _patched(
            _ok_payload([]), prov=prov,
            note_readers=[self._reader("s-1")],
            commits_by_sid={"s-1": [{"sha": "abc"}]},
        ):
            out = tc.gather_completeness_evidence("t-abc123")

        roles = out["session_evidence"][0]["roles"]
        assert set(roles) == {"developed", "note_reader"}

    def test_cache_note_reports_readers(self):
        with _patched(
            _ok_payload([]), prov=_prov(),
            note_readers=[self._reader("s-reader")],
            commits_by_sid={"s-reader": [{"sha": "abc"}]},
        ):
            out = tc.gather_completeness_evidence("t-abc123")

        assert "note-read awareness" in out["cache_note"].lower()
        assert "1 session" in out["cache_note"].lower()

    def test_readers_scan_failure_degrades(self):
        with _patched(_ok_payload([]), prov=_prov(),
                      readers_exc=RuntimeError("scan boom")):
            out = tc.gather_completeness_evidence("t-abc123")

        assert out["status"] == "degraded"
        assert any(e["step"] == "note_readers" for e in out["errors"])
