"""First-class task↔session provenance roles.

Covers:
- ``created_by_session`` capture + accessor (migration v11).
- ``backfill_created_by`` note-prose parser (match / none / ambiguous / dry-run).
- ``provenance`` developed-by derivation (commit-ref Rung 2, assigned Rung 1,
  multi-task-id), the note-read awareness ladder, and the unified
  ``build_task_provenance`` shape + Rung-3 signpost.

DB isolation mirrors ``test_session_tasks_reverse_query`` (store) and
``test_session_prs_ingest`` (conversation-observability + fake projects dir).
"""

from __future__ import annotations

import pytest

from work_buddy.obsidian.tasks import provenance, store
from tests.unit.conversation_observability_fixtures import (
    assistant_text,
    assistant_write,
    commit_scenario,
    user_turn,
    write_session,
)


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "tasks.sqlite3"
    monkeypatch.setattr(store, "_db_path", lambda: db)
    yield db


@pytest.fixture()
def co_env(tmp_path, monkeypatch):
    """Isolated fake ~/.claude/projects + conversation-observability DB."""
    projects = tmp_path / "projects"
    projects.mkdir()
    db_file = tmp_path / "co.db"
    from work_buddy.sessions import inspector

    monkeypatch.setattr(inspector, "_CLAUDE_PROJECTS", projects)
    monkeypatch.setattr(
        "work_buddy.conversation_observability.db._default_db_path",
        lambda: db_file,
    )
    monkeypatch.setattr(
        "work_buddy.conversation_observability.db.db_path",
        lambda cfg=None: db_file,
    )
    return {"projects": projects, "db": db_file}


def _seed_commit(co_env, session_id, *, message, commit_hash="aaa1111"):
    """Synthesize a one-commit session and ingest it into session_commits."""
    from work_buddy.conversation_observability import commits as commits_mod

    write_session(
        co_env["projects"] / "p",
        session_id,
        commit_scenario(session_id, message=message, commit_hash=commit_hash),
    )
    commits_mod.refresh_session_commits(days=3650)


# ── created_by capture + accessor ───────────────────────────────────


def test_created_by_stored_and_read(fresh_db) -> None:
    store.create(task_id="t-aaaaaaaa", created_by_session="real-jsonl-uuid")
    assert store.get_created_by("t-aaaaaaaa") == "real-jsonl-uuid"


def test_created_by_null_when_unrecorded(fresh_db) -> None:
    store.create(task_id="t-bbbbbbbb")
    assert store.get_created_by("t-bbbbbbbb") is None


def test_created_by_sidecar_stored_verbatim(fresh_db) -> None:
    # We never fabricate or rewrite — a bootstrap/sidecar id is stored as-is.
    store.create(task_id="t-cccccccc", created_by_session="sidecar-4bf935c6")
    assert store.get_created_by("t-cccccccc") == "sidecar-4bf935c6"


def test_migration_v11_column_present(fresh_db) -> None:
    store.create(task_id="t-dddddddd")
    assert "created_by_session" in store.get("t-dddddddd")


def test_get_created_by_unknown_task(fresh_db) -> None:
    assert store.get_created_by("t-nonexist") is None


# ── backfill from note prose ────────────────────────────────────────


def test_backfill_extract_single_match() -> None:
    from work_buddy.obsidian.tasks import backfill_created_by as bf

    body = "intro\nThis is a handoff prompt from session 9474f4c7-2a88-47fe-93c1-76e8fb67c49b\n"
    assert bf._extract_creator_session(body) == "9474f4c7-2a88-47fe-93c1-76e8fb67c49b"


def test_backfill_extract_no_match() -> None:
    from work_buddy.obsidian.tasks import backfill_created_by as bf

    assert bf._extract_creator_session("no handoff line here") is None


def test_backfill_extract_ambiguous() -> None:
    from work_buddy.obsidian.tasks import backfill_created_by as bf

    body = (
        "handoff prompt from session aaaaaaaa-1111-2222-3333-444444444444\n"
        "handoff prompt from session bbbbbbbb-1111-2222-3333-444444444444\n"
    )
    assert isinstance(bf._extract_creator_session(body), bf._Ambiguous)


def test_backfill_updates_unambiguous(fresh_db, monkeypatch) -> None:
    from work_buddy.obsidian.tasks import backfill_created_by as bf

    store.create(task_id="t-eeeeeeee", note_uuid="note-1")
    monkeypatch.setattr(
        bf, "_read_note",
        lambda u: "handoff prompt from session ffffffff-0000-1111-2222-333333333333\n",
    )
    report = bf.backfill_created_by()
    assert "t-eeeeeeee" in report["updated"]
    assert store.get_created_by("t-eeeeeeee") == "ffffffff-0000-1111-2222-333333333333"


def test_backfill_skips_ambiguous_leaves_null(fresh_db, monkeypatch) -> None:
    from work_buddy.obsidian.tasks import backfill_created_by as bf

    store.create(task_id="t-ffffffff", note_uuid="note-2")
    monkeypatch.setattr(
        bf, "_read_note",
        lambda u: (
            "handoff prompt from session aaaaaaaa1\n"
            "handoff prompt from session bbbbbbbb2\n"
        ),
    )
    report = bf.backfill_created_by()
    assert "t-ffffffff" in report["skipped_ambiguous"]
    assert store.get_created_by("t-ffffffff") is None


def test_backfill_dry_run_does_not_write(fresh_db, monkeypatch) -> None:
    from work_buddy.obsidian.tasks import backfill_created_by as bf

    store.create(task_id="t-12121212", note_uuid="note-3")
    monkeypatch.setattr(
        bf, "_read_note", lambda u: "handoff prompt from session deadbeef-cafe\n"
    )
    report = bf.backfill_created_by(dry_run=True)
    assert "t-12121212" in report["updated"]  # reported as would-update
    assert store.get_created_by("t-12121212") is None  # but not written


def test_backfill_skips_rows_already_populated(fresh_db, monkeypatch) -> None:
    # Idempotent: an already-populated row is not even scanned.
    store.create(task_id="t-34343434", note_uuid="note-4", created_by_session="set-already")
    from work_buddy.obsidian.tasks import backfill_created_by as bf

    called = []
    monkeypatch.setattr(bf, "_read_note", lambda u: called.append(u) or "")
    report = bf.backfill_created_by()
    assert report["scanned"] == 0
    assert called == []


# ── developed_by derivation (commit-ref) ────────────────────────────


def test_developed_by_commit_ref_unassigned_is_rung2(fresh_db, co_env) -> None:
    store.create(task_id="t-aaaaaaaa")
    _seed_commit(co_env, "s-dev1", message="feat: implement it (t-aaaaaaaa)")

    dev = provenance.build_developed_by("t-aaaaaaaa", include_awareness=False)
    entry = next(e for e in dev if e["session_id"] == "s-dev1")
    assert entry["rung"] == 2
    assert entry["provenance"] == "commit-ref"
    assert entry["evidence"] and entry["evidence"][0]["kind"] == "commit"


def test_developed_by_assigned_committer_is_rung1(fresh_db, co_env) -> None:
    store.create(task_id="t-bbbbbbbb")
    store.assign_session("t-bbbbbbbb", "s-dev2")
    _seed_commit(co_env, "s-dev2", message="fix: done (t-bbbbbbbb)")

    b = provenance.build_task_provenance("t-bbbbbbbb", include_awareness=False)
    entry = next(e for e in b["developed_by"] if e["session_id"] == "s-dev2")
    assert entry["rung"] == 1
    assert entry["provenance"] == "assigned+commit"
    assert entry["awareness"] == "assigned"


def test_developed_by_multi_task_id_attributes_to_both(fresh_db, co_env) -> None:
    store.create(task_id="t-aaaaaaaa")
    store.create(task_id="t-bbbbbbbb")
    _seed_commit(co_env, "s-multi", message="chore: both (t-aaaaaaaa) (t-bbbbbbbb)")

    a = {e["session_id"] for e in provenance.build_developed_by("t-aaaaaaaa", include_awareness=False)}
    b = {e["session_id"] for e in provenance.build_developed_by("t-bbbbbbbb", include_awareness=False)}
    assert "s-multi" in a
    assert "s-multi" in b


def test_developed_by_empty_when_no_commit_ref(fresh_db, co_env) -> None:
    store.create(task_id="t-aaaaaaaa")
    _seed_commit(co_env, "s-other", message="unrelated subject, no id")
    assert provenance.build_developed_by("t-aaaaaaaa", include_awareness=False) == []


# ── awareness ladder ────────────────────────────────────────────────


def test_awareness_read_note_via_read_tool(co_env) -> None:
    note_uuid = "uuid-rn"
    write_session(
        co_env["projects"] / "p", "s-rn",
        [
            user_turn("hi", "2026-05-13T10:00:00Z"),
            assistant_write("Read", f"tasks/notes/{note_uuid}.md", "tu1", "2026-05-13T10:00:01Z"),
        ],
    )
    assert provenance.session_awareness_of_task("s-rn", "t-aaaaaaaa", note_uuid) == "read_note"


def test_awareness_read_note_via_task_read_mcp(co_env) -> None:
    tid = "t-aaaaaaaa"
    entry = {
        "type": "assistant",
        "timestamp": "2026-05-13T10:00:00Z",
        "message": {"content": [{
            "type": "tool_use", "id": "tu", "name": "mcp__work-buddy__wb_run",
            "input": {"capability": "task_read", "params": {"task_id": tid}},
        }]},
    }
    write_session(co_env["projects"] / "p", "s-tr", [entry])
    assert provenance.session_awareness_of_task("s-tr", tid, "uuid-x") == "read_note"


def test_awareness_saw_id_only(co_env) -> None:
    tid = "t-aaaaaaaa"
    write_session(
        co_env["projects"] / "p", "s-saw",
        [user_turn(f"working on {tid} today", "2026-05-13T10:00:00Z")],
    )
    assert provenance.session_awareness_of_task("s-saw", tid, "uuid-y") == "saw_id"


def test_awareness_none_when_unmentioned(co_env) -> None:
    write_session(
        co_env["projects"] / "p", "s-none",
        [assistant_text("totally unrelated work", "2026-05-13T10:00:00Z")],
    )
    assert provenance.session_awareness_of_task("s-none", "t-aaaaaaaa", "uuid-z") == "none"


def test_awareness_no_transcript_when_session_absent(co_env) -> None:
    # Session was never written → no JSONL → can't tell (not "none").
    assert provenance.session_awareness_of_task("ghost-session", "t-aaaaaaaa", "uuid-q") == "no_transcript"


# ── classification ──────────────────────────────────────────────────


def test_classify_levels() -> None:
    assert provenance._classify("assigned") == "informed"
    assert provenance._classify("read_note") == "informed"
    assert provenance._classify("none") == "convergent"
    assert provenance._classify("saw_id") == "unknown"
    assert provenance._classify("no_transcript") == "unknown"
    assert provenance._classify("not_computed") == "unknown"


# ── unified read surface + Rung-3 signpost ──────────────────────────


def test_build_task_provenance_shape(fresh_db, co_env) -> None:
    store.create(task_id="t-aaaaaaaa")
    b = provenance.build_task_provenance("t-aaaaaaaa")
    assert set(b) == {"task_id", "created_by", "assigned", "developed_by", "intent_attribution"}
    assert b["created_by"] is None
    assert b["assigned"] == []
    assert b["developed_by"] == []
    # Rung-3 is never precomputed.
    assert b["intent_attribution"]["computed"] is False
    assert b["intent_attribution"]["hook"] == "wb-task-completeness"


def test_build_task_provenance_includes_created_by(fresh_db, co_env) -> None:
    store.create(task_id="t-bbbbbbbb", created_by_session="creator-sess")
    b = provenance.build_task_provenance("t-bbbbbbbb")
    assert b["created_by"] == "creator-sess"


def test_commit_ref_developer_classifies_informed_when_note_read(fresh_db, co_env) -> None:
    """End-to-end: a session that committed referencing the task AND read
    its note resolves to a Rung-2, informed developer."""
    note_uuid = "uuid-e2e"
    store.create(task_id="t-aaaaaaaa", note_uuid=note_uuid)
    # One session: reads the note, then commits referencing the task.
    write_session(
        co_env["projects"] / "p", "s-e2e",
        [
            assistant_write("Read", f"tasks/notes/{note_uuid}.md", "tuR", "2026-05-13T10:00:00Z"),
            *commit_scenario("s-e2e", message="feat: ship it (t-aaaaaaaa)", commit_hash="bbb2222"),
        ],
    )
    from work_buddy.conversation_observability import commits as commits_mod
    commits_mod.refresh_session_commits(days=3650)

    b = provenance.build_task_provenance("t-aaaaaaaa")
    entry = next(e for e in b["developed_by"] if e["session_id"] == "s-e2e")
    assert entry["rung"] == 2
    assert entry["awareness"] == "read_note"
    assert entry["classification"] == "informed"


# ── per-session inverse (dashboard Tasks rail) ──────────────────────


def test_session_roles_created(fresh_db, co_env) -> None:
    store.create(task_id="t-aaaaaaaa", created_by_session="s-roles", description="X")
    result = provenance.build_session_task_roles("s-roles")
    t = next(t for t in result["tasks"] if t["task_id"] == "t-aaaaaaaa")
    assert t["roles"] == ["created"]
    assert t["task_text"] == "X"


def test_session_roles_assigned(fresh_db, co_env) -> None:
    store.create(task_id="t-bbbbbbbb")
    store.assign_session("t-bbbbbbbb", "s-roles")
    t = next(t for t in provenance.build_session_task_roles("s-roles")["tasks"] if t["task_id"] == "t-bbbbbbbb")
    assert "assigned" in t["roles"] and t["assigned_at"] is not None


def test_session_roles_developed(fresh_db, co_env) -> None:
    store.create(task_id="t-cccccccc")
    _seed_commit(co_env, "s-roles", message="feat: did it (t-cccccccc)")
    t = next(t for t in provenance.build_session_task_roles("s-roles")["tasks"] if t["task_id"] == "t-cccccccc")
    assert t["roles"] == ["developed"]


def test_session_roles_multi(fresh_db, co_env) -> None:
    # One session both created and developed the same task.
    store.create(task_id="t-dddddddd", created_by_session="s-roles")
    _seed_commit(co_env, "s-roles", message="feat: ship (t-dddddddd)")
    t = next(t for t in provenance.build_session_task_roles("s-roles")["tasks"] if t["task_id"] == "t-dddddddd")
    assert t["roles"] == ["created", "developed"]


def test_session_roles_empty_for_unknown(fresh_db, co_env) -> None:
    assert provenance.build_session_task_roles("nobody")["tasks"] == []
