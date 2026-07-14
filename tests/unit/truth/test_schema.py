"""Schema and migration tests for the truth ledger."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import work_buddy.truth.migrations as truth_migrations
from work_buddy.storage.migrations import (
    Migration,
    MigrationError,
    SchemaVersionTooNew,
)


NOW = "2026-07-14T12:00:00.000+00:00"


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _insert_store_info(conn: sqlite3.Connection, version: int = 1) -> None:
    conn.execute(
        "INSERT INTO store_info "
        "(store_id, profile, schema_version, title, created_at) "
        "VALUES ('store-1', 'test', ?, 'Test store', ?)",
        (version, NOW),
    )


def _seed_all_tables(conn: sqlite3.Connection) -> None:
    _insert_store_info(conn)
    conn.execute(
        "INSERT INTO ledger_records (record_type, record_key) "
        "VALUES ('claim', 'c1')"
    )
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
        "author_kind, author_ref, redacted_at, created_at, "
        "created_by_kind, created_by_ref) "
        "VALUES ('sp1', 'e1', '{\"exact\":\"source\"}', 'source', "
        "'hash-sp1', 'human', 'user-1', NULL, ?, 'human', 'user-1')",
        (NOW,),
    )
    for claim_id, proposition, digest in (
        ("c1", "One", "hash-c1"),
        ("c2", "Two", "hash-c2"),
    ):
        conn.execute(
            "INSERT INTO claims "
            "(id, proposition, canonical_sha256, claim_kind, "
            "structured_json, scope, valid_from, valid_to, "
            "confidence_extraction, meta_json, redacted_at, created_at, "
            "created_by_kind, created_by_ref) "
            "VALUES (?, ?, ?, 'fact', '{}', 'store', NULL, NULL, 0.9, "
            "'{}', NULL, ?, 'human', 'user-1')",
            (claim_id, proposition, digest, NOW),
        )
    conn.execute(
        "INSERT INTO derivations "
        "(id, claim_id, method, producer_kind, producer_ref, confidence, "
        "rationale, created_at) "
        "VALUES ('d1', 'c1', 'entailment', 'system', 'test', 0.8, "
        "'because', ?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO derivation_premises "
        "(derivation_id, premise_kind, premise_ref) "
        "VALUES ('d1', 'local', 'c2')"
    )
    conn.execute(
        "INSERT INTO claim_links "
        "(id, from_claim_id, link_type, to_kind, to_ref, role_json, "
        "target_fingerprint, fingerprint_reviewed_at, created_at, "
        "created_by_kind, created_by_ref) "
        "VALUES ('l1', 'c1', 'supersedes', 'claim', 'c2', "
        "'{\"supersession_reason\":\"updated\"}', NULL, NULL, ?, "
        "'human', 'user-1')",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO link_retractions "
        "(link_id, at, actor_kind, actor_ref, reason) "
        "VALUES ('l1', ?, 'human', 'user-1', 'mistake')",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO claim_status_events "
        "(id, claim_id, status, at, actor_kind, actor_ref, basis_kind, "
        "basis_ref, note) "
        "VALUES ('se1', 'c1', 'proposed', ?, 'human', 'user-1', "
        "'import', 'fixture', 'created')",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO gestures "
        "(id, at, surface, actor_ref, kind, subject_ref, payload_sha256, "
        "payload_excerpt, context_sha256, expires_at, consumed_at) "
        "VALUES ('g1', ?, 'dashboard', 'user-1', 'confirm', 'c1', "
        "'hash-c1', 'One', NULL, NULL, NULL)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO redaction_events "
        "(id, subject_kind, subject_ref, at, actor_ref, basis_kind, "
        "basis_ref, reason) "
        "VALUES ('r1', 'claim', 'c2', ?, 'user-1', 'gesture', "
        "'g1', 'privacy')",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO projections "
        "(id, path, rendered_at, content_sha256, manifest_json, health, "
        "health_reason) "
        "VALUES ('p1', 'canon.md', ?, 'projection-hash', '[]', 'clean', NULL)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO sweeps (id, kind, at, params_json) "
        "VALUES ('sw1', 'integrity', ?, '{}')",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO sweep_findings "
        "(id, sweep_id, subject_kind, subject_ref, finding, resolved_at, "
        "resolved_by_ref) "
        "VALUES ('sf1', 'sw1', 'claim', 'c1', 'needs_review', NULL, NULL)"
    )
    conn.execute(
        "INSERT INTO claims_current "
        "(claim_id, status, status_seq, effective_valid_from, "
        "effective_valid_to, health, health_reason, rebuilt_at) "
        "VALUES ('c1', 'proposed', 1, NULL, NULL, 'clean', NULL, ?)",
        (NOW,),
    )
    conn.commit()


@pytest.fixture
def migrated_db(tmp_path: Path):
    path = tmp_path / "store.db"
    conn = _connect(path)
    assert truth_migrations.migrate(conn, path) == 1
    _seed_all_tables(conn)
    try:
        yield conn, path
    finally:
        conn.close()


EXPECTED_COLUMNS = {
    "store_info": {
        "store_id", "profile", "schema_version", "title", "created_at",
    },
    "ledger_records": {"seq", "record_type", "record_key"},
    "evidence": {
        "id", "kind", "source_locator", "content_sha256", "content",
        "content_path", "media_type", "acquired_at", "acquired_by_kind",
        "acquired_by_ref", "acquisition_method", "trust_class",
        "derived_from_store", "meta_json", "redacted_at", "created_at",
    },
    "evidence_spans": {
        "id", "evidence_id", "selector_json", "quote_exact", "span_sha256",
        "author_kind", "author_ref", "redacted_at", "created_at",
        "created_by_kind", "created_by_ref",
    },
    "claims": {
        "id", "proposition", "canonical_sha256", "claim_kind",
        "structured_json", "scope", "valid_from", "valid_to",
        "confidence_extraction", "meta_json", "redacted_at", "created_at",
        "created_by_kind", "created_by_ref",
    },
    "derivations": {
        "id", "claim_id", "method", "producer_kind", "producer_ref",
        "confidence", "rationale", "created_at",
    },
    "derivation_premises": {
        "derivation_id", "premise_kind", "premise_ref",
    },
    "claim_links": {
        "id", "from_claim_id", "link_type", "to_kind", "to_ref",
        "role_json", "target_fingerprint", "fingerprint_reviewed_at",
        "created_at", "created_by_kind", "created_by_ref",
    },
    "link_retractions": {
        "link_id", "at", "actor_kind", "actor_ref", "reason",
    },
    "claim_status_events": {
        "seq", "id", "claim_id", "status", "at", "actor_kind",
        "actor_ref", "basis_kind", "basis_ref", "note",
    },
    "gestures": {
        "id", "at", "surface", "actor_ref", "kind", "subject_ref",
        "payload_sha256", "payload_excerpt", "context_sha256", "expires_at",
        "consumed_at",
    },
    "redaction_events": {
        "id", "subject_kind", "subject_ref", "at", "actor_ref",
        "basis_kind", "basis_ref", "reason",
    },
    "projections": {
        "id", "path", "rendered_at", "content_sha256", "manifest_json",
        "health", "health_reason",
    },
    "sweeps": {"id", "kind", "at", "params_json"},
    "sweep_findings": {
        "id", "sweep_id", "subject_kind", "subject_ref", "finding",
        "resolved_at", "resolved_by_ref",
    },
    "claims_current": {
        "claim_id", "status", "status_seq", "effective_valid_from",
        "effective_valid_to", "health", "health_reason", "rebuilt_at",
    },
}


def test_schema_v1_has_all_committed_tables_columns_indexes_and_triggers(
    migrated_db,
):
    conn, _ = migrated_db
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' AND name != '_migration_history'"
        )
    }
    assert tables == set(EXPECTED_COLUMNS)
    for table, expected in EXPECTED_COLUMNS.items():
        columns = {
            row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
        }
        assert columns == expected

    index_rows = conn.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type = 'index' AND sql IS NOT NULL"
    ).fetchall()
    indexes = {row["name"]: row["sql"] for row in index_rows}
    assert {
        "idx_claim_status_claim_at",
        "idx_claim_status_claim_seq",
        "uq_claim_status_confirm_gesture",
        "idx_claim_links_from",
        "idx_claim_links_target",
        "idx_claims_scope_kind",
        "idx_claims_scope_valid_from",
        "idx_claims_canonical_sha256",
        "idx_evidence_content_sha256",
        "idx_evidence_spans_evidence",
        "idx_sweep_findings_sweep",
    } <= set(indexes)
    assert "WHERE status = 'confirmed'" in indexes[
        "uq_claim_status_confirm_gesture"
    ]

    triggers = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        )
    }
    assert len(triggers) == 29
    assert not any(name.startswith("projections_") for name in triggers)
    assert not any(name.startswith("claims_current_") for name in triggers)
    assert truth_migrations.current_version(conn) == truth_migrations.SCHEMA_VERSION


def test_reopening_is_idempotent(tmp_path: Path):
    path = tmp_path / "store.db"
    conn = _connect(path)
    for _ in range(6):
        assert truth_migrations.migrate(conn, path) == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM _migration_history"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger'"
    ).fetchone()[0] == 29
    conn.close()


INVALID_UPDATES = (
    "UPDATE store_info SET title = 'changed' WHERE store_id = 'store-1'",
    "UPDATE ledger_records SET record_key = 'changed' WHERE seq = 1",
    "UPDATE evidence SET meta_json = '{\"x\":1}' WHERE id = 'e1'",
    "UPDATE evidence_spans SET selector_json = '{}' WHERE id = 'sp1'",
    "UPDATE claims SET proposition = 'changed' WHERE id = 'c1'",
    "UPDATE derivations SET method = 'calculation' WHERE id = 'd1'",
    "UPDATE derivation_premises SET premise_kind = 'uri' "
    "WHERE derivation_id = 'd1' AND premise_ref = 'c2'",
    "UPDATE claim_links SET role_json = '{}' WHERE id = 'l1'",
    "UPDATE link_retractions SET reason = 'changed' WHERE link_id = 'l1'",
    "UPDATE claim_status_events SET note = 'changed' WHERE id = 'se1'",
    "UPDATE gestures SET payload_excerpt = 'changed' WHERE id = 'g1'",
    "UPDATE redaction_events SET reason = 'changed' WHERE id = 'r1'",
    "UPDATE sweeps SET kind = 'freshness' WHERE id = 'sw1'",
    "UPDATE sweep_findings SET finding = 'changed' WHERE id = 'sf1'",
)


@pytest.mark.parametrize("sql", INVALID_UPDATES)
def test_non_sanctioned_updates_are_rejected(migrated_db, sql: str):
    conn, _ = migrated_db
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(sql)
    conn.rollback()


@pytest.mark.parametrize(
    "table",
    (
        "store_info",
        "ledger_records",
        "evidence",
        "evidence_spans",
        "claims",
        "derivations",
        "derivation_premises",
        "claim_links",
        "link_retractions",
        "claim_status_events",
        "gestures",
        "redaction_events",
        "sweeps",
        "sweep_findings",
    ),
)
def test_base_table_deletes_are_rejected(migrated_db, table: str):
    conn, _ = migrated_db
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(f"DELETE FROM {table}")
    conn.rollback()


def test_every_sanctioned_mutation_is_allowed(migrated_db):
    conn, _ = migrated_db
    conn.execute(
        "UPDATE evidence SET content = NULL, content_path = NULL, "
        "redacted_at = ? WHERE id = 'e1'",
        (NOW,),
    )
    conn.execute(
        "UPDATE evidence_spans SET quote_exact = NULL, redacted_at = ? "
        "WHERE id = 'sp1'",
        (NOW,),
    )
    conn.execute(
        "UPDATE claims SET proposition = '[redacted]', structured_json = NULL, "
        "redacted_at = ? WHERE id = 'c1'",
        (NOW,),
    )
    conn.execute(
        "UPDATE gestures SET consumed_at = ? WHERE id = 'g1'",
        (NOW,),
    )
    conn.execute(
        "UPDATE sweep_findings SET resolved_at = ?, resolved_by_ref = 'user-1' "
        "WHERE id = 'sf1'",
        (NOW,),
    )
    conn.execute(
        "UPDATE projections SET health = 'drifted', "
        "health_reason = 'file changed' WHERE id = 'p1'"
    )
    conn.execute(
        "UPDATE claims_current SET status = 'needs_review', status_seq = 2, "
        "health = 'flagged', health_reason = 'sweep' WHERE claim_id = 'c1'"
    )
    conn.execute("DELETE FROM projections WHERE id = 'p1'")
    conn.execute("DELETE FROM claims_current WHERE claim_id = 'c1'")
    conn.commit()

    assert tuple(conn.execute(
        "SELECT content, content_path FROM evidence WHERE id = 'e1'"
    ).fetchone()) == (None, None)
    assert tuple(conn.execute(
        "SELECT proposition, canonical_sha256 FROM claims WHERE id = 'c1'"
    ).fetchone()) == ("[redacted]", "hash-c1")
    assert conn.execute(
        "SELECT consumed_at FROM gestures WHERE id = 'g1'"
    ).fetchone()[0] == NOW
    assert conn.execute(
        "SELECT resolved_by_ref FROM sweep_findings WHERE id = 'sf1'"
    ).fetchone()[0] == "user-1"

    conn.execute("BEGIN")
    conn.execute(
        "UPDATE store_info SET schema_version = 2 WHERE store_id = 'store-1'"
    )
    assert conn.execute(
        "SELECT schema_version FROM store_info"
    ).fetchone()[0] == 2
    conn.rollback()


def test_store_info_rejects_a_second_row(migrated_db):
    conn, _ = migrated_db
    with pytest.raises(sqlite3.IntegrityError, match="store-info-single-row"):
        conn.execute(
            "INSERT INTO store_info "
            "(store_id, profile, schema_version, created_at) "
            "VALUES ('store-2', 'test', 1, ?)",
            (NOW,),
        )
    conn.rollback()


def test_one_gesture_can_confirm_only_one_claim(migrated_db):
    conn, _ = migrated_db
    conn.execute(
        "INSERT INTO claim_status_events "
        "(id, claim_id, status, at, actor_kind, actor_ref, basis_kind, basis_ref) "
        "VALUES ('se-confirm-1', 'c1', 'confirmed', ?, 'human', 'user-1', "
        "'gesture', 'g1')",
        (NOW,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        conn.execute(
            "INSERT INTO claim_status_events "
            "(id, claim_id, status, at, actor_kind, actor_ref, basis_kind, "
            "basis_ref) VALUES ('se-confirm-2', 'c2', 'confirmed', ?, "
            "'human', 'user-1', 'gesture', 'g1')",
            (NOW,),
        )
    conn.rollback()


def test_canonical_hash_index_is_not_unique(migrated_db):
    conn, _ = migrated_db
    for claim_id in ("same-1", "same-2"):
        conn.execute(
            "INSERT INTO claims "
            "(id, proposition, canonical_sha256, claim_kind, scope, created_at, "
            "created_by_kind) VALUES (?, 'Same', 'same-hash', 'fact', "
            "'store', ?, 'human')",
            (claim_id, NOW),
        )
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) FROM claims WHERE canonical_sha256 = 'same-hash'"
    ).fetchone()[0] == 2
    index = {
        row["name"]: row["unique"]
        for row in conn.execute("PRAGMA index_list(claims)")
    }
    assert index["idx_claims_canonical_sha256"] == 0


def test_ledger_records_supply_one_global_append_order(migrated_db):
    conn, _ = migrated_db
    first = conn.execute(
        "INSERT INTO ledger_records (record_type, record_key) VALUES (?, ?)",
        ("evidence", "e2"),
    ).lastrowid
    second = conn.execute(
        "INSERT INTO ledger_records (record_type, record_key) VALUES (?, ?)",
        ("claim_status_event", "se2"),
    ).lastrowid

    assert second == first + 1
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        conn.execute(
            "INSERT INTO ledger_records (record_type, record_key) VALUES (?, ?)",
            ("evidence", "e2"),
        )
    conn.rollback()


def _m002_add_marker(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE migration_v2_marker (id TEXT PRIMARY KEY)")


def _m002_fail(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE rolled_back_marker (id TEXT PRIMARY KEY)")
    conn.execute("UPDATE store_info SET schema_version = 2")
    raise RuntimeError("synthetic migration failure")


def _m003_add_marker(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE migration_v3_marker (id TEXT PRIMARY KEY)")


def _v2_runner(fn) -> truth_migrations._TruthMigrationRunner:
    return truth_migrations._TruthMigrationRunner(
        "truth",
        migrations=[
            Migration(
                1,
                "initial truth ledger schema",
                truth_migrations._m001_initial_schema,
            ),
            Migration(2, "synthetic v2", fn),
        ],
    )


def _v3_runner() -> truth_migrations._TruthMigrationRunner:
    return truth_migrations._TruthMigrationRunner(
        "truth",
        migrations=[
            Migration(
                1,
                "initial truth ledger schema",
                truth_migrations._m001_initial_schema,
            ),
            Migration(2, "synthetic v2", _m002_add_marker),
            Migration(3, "synthetic v3", _m003_add_marker),
        ],
    )


def test_failed_migration_rolls_back_schema_and_both_versions(
    tmp_path: Path,
    monkeypatch,
):
    path = tmp_path / "store.db"
    conn = _connect(path)
    assert truth_migrations.migrate(conn, path) == 1
    _insert_store_info(conn)
    conn.commit()
    monkeypatch.setattr(
        truth_migrations,
        "TRUTH_MIGRATIONS",
        _v2_runner(_m002_fail),
    )

    with pytest.raises(RuntimeError, match="synthetic migration failure"):
        truth_migrations.migrate(conn, path, snapshot=False)

    assert truth_migrations.current_version(conn) == 1
    assert conn.execute(
        "SELECT schema_version FROM store_info"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'rolled_back_marker'"
    ).fetchone() is None
    assert conn.execute(
        "SELECT COUNT(*) FROM _migration_history"
    ).fetchone()[0] == 1
    conn.close()


def test_snapshot_precedes_synthetic_v2_migration(tmp_path: Path, monkeypatch):
    path = tmp_path / "store.db"
    conn = _connect(path)
    assert truth_migrations.migrate(conn, path) == 1
    _insert_store_info(conn)
    conn.commit()
    monkeypatch.setattr(
        truth_migrations,
        "TRUTH_MIGRATIONS",
        _v2_runner(_m002_add_marker),
    )

    assert truth_migrations.migrate(conn, path) == 2
    snapshot = tmp_path / "store.pre-v1.db"
    assert snapshot.exists()
    assert truth_migrations.current_version(conn) == 2
    assert conn.execute(
        "SELECT schema_version FROM store_info"
    ).fetchone()[0] == 2
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'migration_v2_marker'"
    ).fetchone() is not None

    old = _connect(snapshot)
    assert old.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert truth_migrations.current_version(old) == 1
    assert old.execute(
        "SELECT schema_version FROM store_info"
    ).fetchone()[0] == 1
    assert old.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'migration_v2_marker'"
    ).fetchone() is None
    old.close()
    conn.close()


def test_each_version_bump_gets_its_own_snapshot(tmp_path: Path, monkeypatch):
    path = tmp_path / "store.db"
    conn = _connect(path)
    assert truth_migrations.migrate(conn, path) == 1
    _insert_store_info(conn)
    conn.commit()
    monkeypatch.setattr(truth_migrations, "TRUTH_MIGRATIONS", _v3_runner())

    assert truth_migrations.migrate(conn, path) == 3
    pre_v1 = tmp_path / "store.pre-v1.db"
    pre_v2 = tmp_path / "store.pre-v2.db"
    assert pre_v1.exists()
    assert pre_v2.exists()

    old_v1 = _connect(pre_v1)
    assert truth_migrations.current_version(old_v1) == 1
    assert old_v1.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'migration_v2_marker'"
    ).fetchone() is None
    old_v1.close()

    old_v2 = _connect(pre_v2)
    assert truth_migrations.current_version(old_v2) == 2
    assert old_v2.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'migration_v2_marker'"
    ).fetchone() is not None
    assert old_v2.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'migration_v3_marker'"
    ).fetchone() is None
    old_v2.close()
    conn.close()


def test_newer_store_version_is_refused_before_snapshot(tmp_path: Path):
    path = tmp_path / "store.db"
    conn = _connect(path)
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    with pytest.raises(SchemaVersionTooNew, match="only knows up to v1"):
        truth_migrations.migrate(conn, path)
    assert not (tmp_path / "store.pre-v2.db").exists()
    conn.close()


def test_partial_v0_schema_is_refused_not_stamped(tmp_path: Path):
    path = tmp_path / "store.db"
    conn = _connect(path)
    conn.execute("CREATE TABLE claims (id TEXT PRIMARY KEY)")
    conn.commit()
    with pytest.raises(MigrationError, match="unversioned partial schema"):
        truth_migrations.migrate(conn, path)
    assert truth_migrations.current_version(conn) == 0
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'store_info'"
    ).fetchone() is None
    conn.close()


def test_store_info_and_pragma_version_mismatch_is_refused(tmp_path: Path):
    path = tmp_path / "store.db"
    conn = _connect(path)
    assert truth_migrations.migrate(conn, path) == 1
    _insert_store_info(conn)
    conn.commit()
    conn.execute("UPDATE store_info SET schema_version = 2")
    conn.commit()
    with pytest.raises(MigrationError, match="does not match"):
        truth_migrations.migrate(conn, path)
    conn.close()
