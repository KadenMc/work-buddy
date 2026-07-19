"""Schema and trigger tests for the co-work document surface (v2)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import work_buddy.truth.migrations as truth_migrations
from work_buddy.storage.migrations import Migration


NOW = "2026-07-17T12:00:00.000+00:00"
REDACTED = truth_migrations.REDACTED_SELECTOR_JSON


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _v1_runner() -> truth_migrations._TruthMigrationRunner:
    return truth_migrations._TruthMigrationRunner(
        "truth",
        migrations=[
            Migration(
                1,
                "initial truth ledger schema",
                truth_migrations._m001_initial_schema,
            ),
        ],
    )


def _insert_store_info(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT INTO store_info "
        "(store_id, profile, schema_version, title, created_at) "
        "VALUES ('store-1', 'cothink-doc', ?, 'Doc store', ?)",
        (version, NOW),
    )


def _seed_document_surface(conn: sqlite3.Connection) -> None:
    """Seed one row in each document-surface table plus a proposal gesture."""
    conn.execute(
        "INSERT INTO documents "
        "(id, path, title, document_class, content_sha256, "
        "ydoc_snapshot_sha256, created_at, created_by_kind, created_by_ref, "
        "meta_json) VALUES ('d1', 'docs/design.md', 'Design', 'co_authored', "
        "'hash-d0', NULL, ?, 'human', 'user-1', '{}')",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO document_spans "
        "(id, document_id, selector_json, quote_exact, span_sha256, "
        "author_kind, author_ref, created_at, created_by_kind, created_by_ref) "
        "VALUES ('ds1', 'd1', '{\"exact\":\"passage\"}', 'passage', "
        "'hash-ds1', 'human', 'user-1', ?, 'human', 'user-1')",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO expressions "
        "(id, document_span_id, claim_ref_kind, claim_ref, role, "
        "claim_canonical_sha256, span_sha256, created_at, created_by_kind, "
        "created_by_ref, meta_json) VALUES ('ex1', 'ds1', 'local', 'c1', "
        "'instantiation', 'hash-c1', 'hash-ds1', ?, 'human', 'user-1', NULL)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO proposals "
        "(id, document_id, base_content_sha256, selector_json, quote_exact, "
        "span_sha256, replacement, rationale, tldr, claim_refs_json, "
        "canonical_sha256, dedup_key, expires_at, created_at, created_by_kind, "
        "created_by_ref, meta_json, redacted_at) VALUES ('p1', 'd1', 'hash-d0', "
        "'{\"exact\":\"old\"}', 'old', 'hash-p1span', 'new', 'because', "
        "'cite it', NULL, 'hash-p1', 'dedup-p1', NULL, ?, 'agent_run', "
        "'run-1', '{}', NULL)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO proposal_status_events "
        "(id, proposal_id, status, decision, at, actor_kind, actor_ref, "
        "basis_kind, basis_ref, note) VALUES ('pse1', 'p1', 'open', NULL, ?, "
        "'system', NULL, 'rule', 'created', NULL)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO doc_events "
        "(id, document_id, kind, at, actor_kind, actor_ref, content_sha256, "
        "ydoc_snapshot_sha256, detail) VALUES ('de1', 'd1', 'registered', ?, "
        "'human', 'user-1', 'hash-d0', NULL, NULL)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO gestures "
        "(id, at, surface, actor_ref, kind, subject_ref, payload_sha256, "
        "payload_excerpt, context_sha256, expires_at, consumed_at) "
        "VALUES ('gp1', ?, 'dashboard', 'user-1', 'confirm', 'p1', 'hash-p1', "
        "'old -> new', NULL, NULL, ?)",
        (NOW, NOW),
    )
    conn.commit()


@pytest.fixture
def doc_db(tmp_path: Path):
    path = tmp_path / "store.db"
    conn = _connect(path)
    assert truth_migrations.migrate(conn, path) == 2
    _insert_store_info(conn, 2)
    _seed_document_surface(conn)
    try:
        yield conn, path
    finally:
        conn.close()


NEW_TABLES = (
    "documents",
    "document_spans",
    "expressions",
    "proposals",
    "proposal_status_events",
    "doc_events",
)

STRICT_APPEND_ONLY_TABLES = (
    "document_spans",
    "expressions",
    "proposal_status_events",
    "doc_events",
)


@pytest.mark.parametrize("table", NEW_TABLES)
def test_new_document_tables_reject_deletes(doc_db, table: str):
    conn, _ = doc_db
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(f"DELETE FROM {table}")
    conn.rollback()


STRICT_UPDATES = (
    "UPDATE document_spans SET quote_exact = 'changed' WHERE id = 'ds1'",
    "UPDATE expressions SET role = 'quote' WHERE id = 'ex1'",
    "UPDATE proposal_status_events SET note = 'changed' WHERE id = 'pse1'",
    "UPDATE doc_events SET detail = 'changed' WHERE id = 'de1'",
)


@pytest.mark.parametrize("sql", STRICT_UPDATES)
def test_strict_append_only_tables_reject_updates(doc_db, sql: str):
    conn, _ = doc_db
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(sql)
    conn.rollback()


def test_documents_pointer_carveout_advances_only_content_addresses(doc_db):
    conn, _ = doc_db
    conn.execute(
        "UPDATE documents SET content_sha256 = 'hash-d1', "
        "ydoc_snapshot_sha256 = 'snap-1' WHERE id = 'd1'"
    )
    conn.commit()
    assert tuple(
        conn.execute(
            "SELECT content_sha256, ydoc_snapshot_sha256 FROM documents "
            "WHERE id = 'd1'"
        ).fetchone()
    ) == ("hash-d1", "snap-1")


DOCUMENT_FORBIDDEN_UPDATES = (
    "UPDATE documents SET path = 'docs/other.md' WHERE id = 'd1'",
    "UPDATE documents SET title = 'Renamed' WHERE id = 'd1'",
    "UPDATE documents SET document_class = 'generated' WHERE id = 'd1'",
    "UPDATE documents SET created_by_ref = 'user-2' WHERE id = 'd1'",
    "UPDATE documents SET meta_json = '{\"x\":1}' WHERE id = 'd1'",
)


@pytest.mark.parametrize("sql", DOCUMENT_FORBIDDEN_UPDATES)
def test_documents_carveout_pins_identity_and_lifecycle_columns(doc_db, sql: str):
    conn, _ = doc_db
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(sql)
    conn.rollback()


def test_proposals_redaction_carveout_nulls_content_and_keeps_hashes(doc_db):
    conn, _ = doc_db
    conn.execute(
        "UPDATE proposals SET quote_exact = NULL, replacement = NULL, "
        "rationale = NULL, tldr = NULL, claim_refs_json = NULL, "
        "selector_json = ?, redacted_at = ? WHERE id = 'p1'",
        (REDACTED, NOW),
    )
    conn.commit()
    row = conn.execute(
        "SELECT quote_exact, replacement, rationale, tldr, claim_refs_json, "
        "selector_json, canonical_sha256, dedup_key, base_content_sha256, "
        "span_sha256, redacted_at FROM proposals WHERE id = 'p1'"
    ).fetchone()
    assert tuple(row) == (
        None,
        None,
        None,
        None,
        None,
        REDACTED,
        "hash-p1",
        "dedup-p1",
        "hash-d0",
        "hash-p1span",
        NOW,
    )


PROPOSAL_FORBIDDEN_UPDATES = (
    # A plain content edit is not a redaction.
    "UPDATE proposals SET rationale = 'changed' WHERE id = 'p1'",
    # A partial redaction that leaves quote context behind is refused.
    "UPDATE proposals SET replacement = NULL, redacted_at = '" + NOW + "' "
    "WHERE id = 'p1'",
    # A redaction that also mutates a pinned hash is refused.
    "UPDATE proposals SET quote_exact = NULL, replacement = NULL, "
    "rationale = NULL, tldr = NULL, claim_refs_json = NULL, "
    "selector_json = '" + REDACTED + "', canonical_sha256 = 'tampered', "
    "redacted_at = '" + NOW + "' WHERE id = 'p1'",
)


@pytest.mark.parametrize("sql", PROPOSAL_FORBIDDEN_UPDATES)
def test_proposals_carveout_rejects_partial_or_tampering_updates(doc_db, sql: str):
    conn, _ = doc_db
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(sql)
    conn.rollback()


def test_gestures_trigger_admits_only_a_redacted_proposal_subject(doc_db):
    conn, _ = doc_db
    # While the proposal subject is live its bound gesture excerpt is sealed.
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE gestures SET payload_excerpt = '[redacted]' WHERE id = 'gp1'"
        )
    conn.rollback()

    conn.execute(
        "UPDATE proposals SET quote_exact = NULL, replacement = NULL, "
        "rationale = NULL, tldr = NULL, claim_refs_json = NULL, "
        "selector_json = ?, redacted_at = ? WHERE id = 'p1'",
        (REDACTED, NOW),
    )
    # Once the proposal is redacted the receipt excerpt may be scrubbed.
    conn.execute(
        "UPDATE gestures SET payload_excerpt = '[redacted]' WHERE id = 'gp1'"
    )
    conn.commit()
    assert (
        conn.execute(
            "SELECT payload_excerpt FROM gestures WHERE id = 'gp1'"
        ).fetchone()[0]
        == "[redacted]"
    )


def test_recreated_gestures_trigger_carries_the_proposal_branch(doc_db):
    conn, _ = doc_db
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
        "AND name = 'gestures_append_only_update'"
    ).fetchone()[0]
    assert "FROM proposals" in sql
    assert "FROM claims" in sql
    assert "FROM evidence" in sql
    assert "FROM evidence_spans" in sql


# --- Adjudication 1: frozen-v1 mutations survive the gestures-trigger recreation.


def _seed_v1_redactable_rows(conn: sqlite3.Connection) -> None:
    _insert_store_info(conn, 1)
    conn.execute(
        "INSERT INTO evidence "
        "(id, kind, source_locator, content_sha256, content, content_path, "
        "media_type, acquired_at, acquired_by_kind, acquired_by_ref, "
        "acquisition_method, trust_class, derived_from_store, meta_json, "
        "redacted_at, created_at) "
        "VALUES ('e1', 'document', 'file:///source.md', 'hash-e1', 'source', "
        "'blobs/hash-e1', 'text/markdown', ?, 'human', 'user-1', 'paste', "
        "'user_authored', NULL, '{}', NULL, ?)",
        (NOW, NOW),
    )
    conn.execute(
        "INSERT INTO evidence_spans "
        "(id, evidence_id, selector_json, quote_exact, span_sha256, "
        "author_kind, author_ref, redacted_at, created_at, created_by_kind, "
        "created_by_ref) VALUES ('sp1', 'e1', '{\"exact\":\"source\"}', "
        "'source', 'hash-sp1', 'human', 'user-1', NULL, ?, 'human', 'user-1')",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO claims "
        "(id, proposition, canonical_sha256, claim_kind, structured_json, "
        "scope, valid_from, valid_to, confidence_extraction, meta_json, "
        "redacted_at, created_at, created_by_kind, created_by_ref) "
        "VALUES ('c1', 'One', 'hash-c1', 'fact', '{}', 'store', NULL, NULL, "
        "0.9, '{}', NULL, ?, 'human', 'user-1')",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO gestures "
        "(id, at, surface, actor_ref, kind, subject_ref, payload_sha256, "
        "payload_excerpt, context_sha256, expires_at, consumed_at) "
        "VALUES ('g1', ?, 'dashboard', 'user-1', 'redact', 'c1', 'hash-c1', "
        "'One', NULL, NULL, NULL)",
        (NOW,),
    )
    conn.commit()


def _build_migrated_v1_store(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "store.db"
    conn = _connect(path)
    try:
        monkeypatch.setattr(truth_migrations, "TRUTH_MIGRATIONS", _v1_runner())
        assert truth_migrations.migrate(conn, path) == 1
        _seed_v1_redactable_rows(conn)
        monkeypatch.undo()
        assert truth_migrations.migrate(conn, path) == 2
    finally:
        conn.close()
    return path


def test_frozen_v1_sanctioned_mutations_survive_migration(tmp_path: Path, monkeypatch):
    path = _build_migrated_v1_store(tmp_path, monkeypatch)
    conn = _connect(path)
    try:
        conn.execute(
            "UPDATE evidence SET content = NULL, content_path = NULL, "
            "redacted_at = ? WHERE id = 'e1'",
            (NOW,),
        )
        conn.execute(
            "UPDATE evidence_spans SET selector_json = ?, quote_exact = NULL, "
            "redacted_at = ? WHERE id = 'sp1'",
            (REDACTED, NOW),
        )
        conn.execute(
            "UPDATE claims SET proposition = '[redacted]', "
            "structured_json = NULL, redacted_at = ? WHERE id = 'c1'",
            (NOW,),
        )
        # The gesture is consumed and then, its claim subject now redacted, its
        # excerpt is scrubbed. This drives the recreated trigger's claim branch.
        conn.execute("UPDATE gestures SET consumed_at = ? WHERE id = 'g1'", (NOW,))
        conn.execute(
            "UPDATE gestures SET payload_excerpt = '[redacted]' WHERE id = 'g1'"
        )
        conn.commit()
        assert tuple(
            conn.execute(
                "SELECT payload_excerpt, payload_sha256 FROM gestures "
                "WHERE id = 'g1'"
            ).fetchone()
        ) == ("[redacted]", "hash-c1")
    finally:
        conn.close()


FROZEN_V1_FORBIDDEN = (
    "UPDATE claims SET proposition = 'changed' WHERE id = 'c1'",
    "UPDATE evidence SET meta_json = '{\"x\":1}' WHERE id = 'e1'",
    "UPDATE evidence_spans SET selector_json = '[]', quote_exact = NULL, "
    "redacted_at = '" + NOW + "' WHERE id = 'sp1'",
    "DELETE FROM claims",
    "DELETE FROM gestures",
)


@pytest.mark.parametrize("sql", FROZEN_V1_FORBIDDEN)
def test_frozen_v1_forbidden_mutations_still_abort_after_migration(
    tmp_path: Path,
    monkeypatch,
    sql: str,
):
    path = _build_migrated_v1_store(tmp_path, monkeypatch)
    conn = _connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(sql)
        conn.rollback()
    finally:
        conn.close()


def test_unbound_gesture_excerpt_redaction_still_aborts_after_migration(
    tmp_path: Path,
    monkeypatch,
):
    path = _build_migrated_v1_store(tmp_path, monkeypatch)
    conn = _connect(path)
    try:
        # The bound claim subject is not redacted, so scrubbing the receipt
        # aborts exactly as it did before the trigger was recreated.
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE gestures SET payload_excerpt = '[redacted]' "
                "WHERE id = 'g1'"
            )
        conn.rollback()
    finally:
        conn.close()
