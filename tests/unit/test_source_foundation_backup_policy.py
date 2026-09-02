from __future__ import annotations

import json
import sqlite3

import pytest

from work_buddy.backups.local import VITAL_DBS, _stage_local_identity_enrollment
from work_buddy.backups.restore import _copy_preserved_source_foundation_state
from work_buddy.backups.source_foundation_policy import (
    SOURCE_FOUNDATION_BACKUP_POLICY,
    validate_source_foundation_backup_policy,
)
from work_buddy.security.local_identity import LocalIdentityAuthority
from work_buddy.sources import SourceStore


FOUNDATION_RESOURCES = {
    "db/agent-execution",
    "db/cowork-conversation-source-dependencies",
    "db/task-note-migration",
    "db/installed-authority",
    "db/local-identity",
    "db/journal-capture",
    "stores/sources",
    "truth-store/document-causality",
}


def test_every_source_foundation_store_has_an_explicit_backup_class() -> None:
    validate_source_foundation_backup_policy(FOUNDATION_RESOURCES)
    assert set(SOURCE_FOUNDATION_BACKUP_POLICY) == FOUNDATION_RESOURCES
    assert VITAL_DBS["agent_execution"] == "db/agent-execution"
    assert VITAL_DBS["cowork_conversation_source_dependencies"] == (
        "db/cowork-conversation-source-dependencies"
    )
    assert VITAL_DBS["task_note_migration"] == "db/task-note-migration"
    assert VITAL_DBS["installed_authority"] == "db/installed-authority"
    assert "db/local-identity" not in VITAL_DBS.values()
    assert "db/journal-capture" not in VITAL_DBS.values()
    assert "stores/sources" not in VITAL_DBS.values()


def test_identity_backup_is_sanitized_and_contains_no_live_authority_rows(
    tmp_path, monkeypatch
) -> None:
    db = tmp_path / "local_identity.db"
    authority = LocalIdentityAuthority(db)
    actor = authority.enrolled_actor()
    sentinel = "RAW_SESSION_OR_GESTURE_SENTINEL"
    with authority._connect() as conn:  # exact fixture setup, not production access
        conn.execute(
            "INSERT INTO local_identity_audit(occurred_at,event_type,object_id,outcome) "
            "VALUES(0,'test',?,'test')",
            (sentinel,),
        )
    monkeypatch.setattr("work_buddy.backups.local.resolve", lambda _resource: db)
    staging = tmp_path / "staging"
    staging.mkdir()
    exported = _stage_local_identity_enrollment(staging)
    assert exported is not None
    raw = exported.read_text(encoding="utf-8")
    assert sentinel not in raw
    payload = json.loads(raw)
    assert payload["issuer_authority_id"] == actor.issuer_authority_id
    assert payload["tenant_scope_id"] == actor.tenant_scope_id
    assert payload["local_actor_id"] == actor.subject
    assert payload["restores_live_sessions"] is False
    assert set(payload) == {
        "schema",
        "schema_version",
        "issuer_authority_id",
        "tenant_scope_id",
        "local_actor_id",
        "restores_live_sessions",
        "trust_required_before_identity_reuse",
    }


def test_restore_preserves_sensitive_live_state_outside_the_archive(tmp_path) -> None:
    live = tmp_path / "db"
    staging = tmp_path / "staging"
    live.mkdir()
    staging.mkdir()
    sources = SourceStore.create(live / "sources")
    source = sources.capture_source(
        content="preserved source",
        source_role="human_input",
        tenant_scope_id="tenant-00000001",
        originating_surface="test",
    )
    for name, value in (
        ("journal_capture.db", "journal"),
        ("local_identity.db", "identity"),
        ("cowork_conversation_source_dependencies.db", "live-deps"),
    ):
        conn = sqlite3.connect(live / name)
        try:
            conn.execute("CREATE TABLE preserved(value TEXT NOT NULL)")
            conn.execute("INSERT INTO preserved(value) VALUES(?)", (value,))
            conn.commit()
        finally:
            conn.close()
    conn = sqlite3.connect(staging / "cowork_conversation_source_dependencies.db")
    try:
        conn.execute("CREATE TABLE stale(value TEXT NOT NULL)")
        conn.commit()
    finally:
        conn.close()

    _copy_preserved_source_foundation_state(live, staging)

    assert SourceStore.open(staging / "sources").get_item(source.source_ref) is not None
    assert SourceStore.open(live / "sources").get_item(source.source_ref) is not None
    for root in (live, staging):
        for name, value in (
            ("journal_capture.db", "journal"),
            ("local_identity.db", "identity"),
            ("cowork_conversation_source_dependencies.db", "live-deps"),
        ):
            conn = sqlite3.connect(root / name)
            try:
                assert conn.execute("SELECT value FROM preserved").fetchone()[0] == value
            finally:
                conn.close()


def test_preserved_state_copy_failure_never_removes_live_authority(
    tmp_path, monkeypatch
) -> None:
    live = tmp_path / "db"
    staging = tmp_path / "staging"
    live.mkdir()
    staging.mkdir()
    (live / "journal_capture.db").write_bytes(b"live-journal")

    def fail_copy(_source, destination):
        destination.write_bytes(b"partial")
        raise RuntimeError("simulated snapshot interruption")

    monkeypatch.setattr(
        "work_buddy.backups.restore._copy_sqlite_snapshot",
        fail_copy,
    )
    with pytest.raises(RuntimeError, match="snapshot interruption"):
        _copy_preserved_source_foundation_state(live, staging)

    assert (live / "journal_capture.db").read_bytes() == b"live-journal"
