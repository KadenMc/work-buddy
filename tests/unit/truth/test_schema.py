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
        "INSERT INTO ledger_records (record_type, record_key) VALUES ('claim', 'c1')"
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
        '\'{"supersession_reason":"updated"}\', NULL, NULL, ?, '
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


def _v1_runner() -> truth_migrations._TruthMigrationRunner:
    """A runner pinned to the v1 schema, for exercising migration machinery."""
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


@pytest.fixture
def migrated_db(tmp_path: Path):
    path = tmp_path / "store.db"
    conn = _connect(path)
    assert truth_migrations.migrate(conn, path) == truth_migrations.SCHEMA_VERSION
    _seed_all_tables(conn)
    try:
        yield conn, path
    finally:
        conn.close()


EXPECTED_COLUMNS = {
    "store_info": {
        "store_id",
        "profile",
        "schema_version",
        "title",
        "created_at",
    },
    "ledger_records": {"seq", "record_type", "record_key"},
    "evidence": {
        "id",
        "kind",
        "source_locator",
        "content_sha256",
        "content",
        "content_path",
        "media_type",
        "acquired_at",
        "acquired_by_kind",
        "acquired_by_ref",
        "acquisition_method",
        "trust_class",
        "derived_from_store",
        "meta_json",
        "redacted_at",
        "created_at",
    },
    "evidence_spans": {
        "id",
        "evidence_id",
        "selector_json",
        "quote_exact",
        "span_sha256",
        "author_kind",
        "author_ref",
        "redacted_at",
        "created_at",
        "created_by_kind",
        "created_by_ref",
    },
    "claims": {
        "id",
        "proposition",
        "canonical_sha256",
        "claim_kind",
        "structured_json",
        "scope",
        "valid_from",
        "valid_to",
        "confidence_extraction",
        "meta_json",
        "redacted_at",
        "created_at",
        "created_by_kind",
        "created_by_ref",
    },
    "derivations": {
        "id",
        "claim_id",
        "method",
        "producer_kind",
        "producer_ref",
        "confidence",
        "rationale",
        "created_at",
    },
    "derivation_premises": {
        "derivation_id",
        "premise_kind",
        "premise_ref",
    },
    "claim_links": {
        "id",
        "from_claim_id",
        "link_type",
        "to_kind",
        "to_ref",
        "role_json",
        "target_fingerprint",
        "fingerprint_reviewed_at",
        "created_at",
        "created_by_kind",
        "created_by_ref",
    },
    "link_retractions": {
        "link_id",
        "at",
        "actor_kind",
        "actor_ref",
        "reason",
    },
    "claim_status_events": {
        "seq",
        "id",
        "claim_id",
        "status",
        "at",
        "actor_kind",
        "actor_ref",
        "basis_kind",
        "basis_ref",
        "note",
    },
    "gestures": {
        "id",
        "at",
        "surface",
        "actor_ref",
        "kind",
        "subject_ref",
        "payload_sha256",
        "payload_excerpt",
        "context_sha256",
        "expires_at",
        "consumed_at",
    },
    "redaction_events": {
        "id",
        "subject_kind",
        "subject_ref",
        "at",
        "actor_ref",
        "basis_kind",
        "basis_ref",
        "reason",
    },
    "projections": {
        "id",
        "path",
        "rendered_at",
        "content_sha256",
        "manifest_json",
        "health",
        "health_reason",
    },
    "sweeps": {"id", "kind", "at", "params_json"},
    "sweep_findings": {
        "id",
        "sweep_id",
        "subject_kind",
        "subject_ref",
        "finding",
        "resolved_at",
        "resolved_by_ref",
    },
    "claims_current": {
        "claim_id",
        "status",
        "status_seq",
        "effective_valid_from",
        "effective_valid_to",
        "health",
        "health_reason",
        "rebuilt_at",
    },
    "documents": {
        "id",
        "path",
        "title",
        "document_class",
        "content_sha256",
        "ydoc_snapshot_sha256",
        "created_at",
        "created_by_kind",
        "created_by_ref",
        "meta_json",
    },
    "document_path_keys": {"document_id", "path_key"},
    "document_versions": {
        "id",
        "document_id",
        "kind",
        "projection_sha256",
        "ydoc_snapshot_sha256",
        "structured_head_sha256",
        "created_at",
        "actor_kind",
        "actor_ref",
        "detail",
    },
    "document_spans": {
        "id",
        "document_id",
        "selector_json",
        "quote_exact",
        "span_sha256",
        "author_kind",
        "author_ref",
        "created_at",
        "created_by_kind",
        "created_by_ref",
    },
    "document_provenance_attestations": {
        "id",
        "document_id",
        "target_kind",
        "document_version_id",
        "document_span_id",
        "target_structured_head_sha256",
        "authorship_kind",
        "human_contributors_json",
        "review_status",
        "human_reviewers_json",
        "source_kind",
        "source_json",
        "basis_kind",
        "basis_ref",
        "supersedes_id",
        "idempotency_key",
        "canonical_sha256",
        "created_at",
        "attested_by_kind",
        "attested_by_ref",
        "attested_by_meta_json",
    },
    "expressions": {
        "id",
        "document_span_id",
        "claim_ref_kind",
        "claim_ref",
        "role",
        "claim_canonical_sha256",
        "span_sha256",
        "created_at",
        "created_by_kind",
        "created_by_ref",
        "meta_json",
    },
    "proposals": {
        "id",
        "document_id",
        "base_content_sha256",
        "base_structured_head_sha256",
        "selector_json",
        "quote_exact",
        "span_sha256",
        "replacement",
        "rationale",
        "tldr",
        "claim_refs_json",
        "canonical_sha256",
        "dedup_key",
        "expires_at",
        "created_at",
        "created_by_kind",
        "created_by_ref",
        "meta_json",
        "redacted_at",
    },
    "proposal_status_events": {
        "seq",
        "id",
        "proposal_id",
        "status",
        "decision",
        "at",
        "actor_kind",
        "actor_ref",
        "basis_kind",
        "basis_ref",
        "note",
    },
    "doc_events": {
        "id",
        "document_id",
        "kind",
        "at",
        "actor_kind",
        "actor_ref",
        "content_sha256",
        "ydoc_snapshot_sha256",
        "detail",
    },
    "cowork_bootstrap_intents": {
        "id",
        "idempotency_key",
        "actor_ref",
        "request_sha256",
        "mode",
        "state",
        "document_id",
        "normalized_path",
        "path_key",
        "title",
        "document_class",
        "source_sha256",
        "source_byte_length",
        "expected_file_sha256",
        "importer_id",
        "source_media_type",
        "import_attestation_sha256",
        "snapshot_sha256",
        "structured_head_sha256",
        "staged_path",
        "created_at",
        "updated_at",
        "expires_at",
        "committed_at",
        "receipt_json",
        "recovery_detail",
    },
    "cowork_materialization_intents": {
        "id",
        "idempotency_key",
        "actor_ref",
        "document_id",
        "state",
        "expected_file_sha256",
        "expected_structured_head_sha256",
        "snapshot_sha256",
        "rendered_sha256",
        "staged_path",
        "quarantine_path",
        "document_version_id",
        "created_at",
        "updated_at",
        "committed_at",
        "receipt_json",
        "recovery_detail",
    },
    "cowork_sitting_intents": {
        "id", "idempotency_key", "actor_ref", "document_id",
        "request_sha256", "state", "expected_file_sha256",
        "expected_structured_head_sha256", "expected_snapshot_sha256",
        "admitted_items_json", "failed_items_json", "has_apply",
        "new_snapshot_sha256", "new_structured_head_sha256",
        "rendered_sha256", "materialization_intent_id", "created_at",
        "updated_at", "expires_at", "committed_at", "receipt_json",
        "recovery_detail",
    },
    "cowork_reimport_intents": {
        "id", "idempotency_key", "actor_ref", "document_id", "state",
        "expected_file_sha256", "prior_projection_sha256",
        "prior_snapshot_sha256", "prior_structured_head_sha256",
        "source_byte_length", "staged_path", "replacement_snapshot_sha256",
        "replacement_structured_head_sha256", "document_version_id",
        "created_at", "updated_at", "expires_at", "committed_at",
        "receipt_json", "recovery_detail",
    },
    "cowork_retirement_intents": {
        "id", "idempotency_key", "actor_ref", "document_id", "state",
        "expected_file_sha256", "expected_projection_sha256",
        "expected_snapshot_sha256", "expected_structured_head_sha256",
        "consequence_sha256", "created_at", "updated_at", "expires_at",
        "committed_at", "receipt_json", "recovery_detail",
    },
    "criterion_definition_versions": {
        "id", "stable_key", "version", "title", "description",
        "criterion_kind", "origin", "configuration_schema_json",
        "canonical_sha256", "created_at", "created_by_kind",
        "created_by_ref", "created_by_meta_json",
    },
    "check_definition_versions": {
        "id", "stable_key", "version", "title", "mechanism",
        "executor_ref", "supported_criterion_kinds_json",
        "input_schema_json", "output_schema_json", "limitations_json",
        "origin", "canonical_sha256", "created_at", "created_by_kind",
        "created_by_ref", "created_by_meta_json",
    },
    "criterion_check_bindings": {
        "id", "criterion_definition_version_id",
        "check_definition_version_id", "configuration_json",
        "canonical_sha256", "created_at", "created_by_kind",
        "created_by_ref", "created_by_meta_json",
    },
    "criterion_activations": {
        "id", "criterion_definition_version_id",
        "criterion_check_binding_id", "scope_json", "is_enabled",
        "is_required", "origin", "canonical_sha256", "created_at",
        "created_by_kind", "created_by_ref", "created_by_meta_json",
    },
    "action_snapshots": {
        "id", "document_id", "document_version_id",
        "ydoc_snapshot_sha256", "structured_head_sha256",
        "ydoc_generation_sha256", "baseline_projection_sha256",
        "projection_sha256", "projection_blob_sha256", "target_kind",
        "target_selector_json", "target_text_sha256", "target_blob_sha256",
        "context_boundary_json", "allowed_change_ranges_json",
        "egress_boundary_json", "canonical_sha256", "created_at",
        "created_by_kind", "created_by_ref", "created_by_meta_json",
    },
    "evaluation_plan_snapshots": {
        "id", "action_snapshot_id", "plan_json", "canonical_sha256",
        "created_at", "created_by_kind", "created_by_ref",
        "created_by_meta_json",
    },
    "evaluation_runs": {
        "id", "action_snapshot_id", "plan_snapshot_id", "run_kind",
        "status", "canonical_sha256", "started_at", "completed_at",
        "created_by_kind", "created_by_ref", "created_by_meta_json",
    },
    "check_executions": {
        "id", "evaluation_run_id", "check_definition_version_id",
        "criterion_check_binding_id", "mechanism", "status",
        "input_sha256", "output_sha256", "diagnostics_json",
        "producer_json", "canonical_sha256", "started_at",
        "completed_at", "created_by_kind", "created_by_ref",
        "created_by_meta_json",
    },
    "evaluation_results": {
        "id", "evaluation_run_id", "check_execution_id",
        "criterion_definition_version_id", "result_kind", "severity",
        "message", "evidence_selector_json", "payload_json",
        "canonical_sha256", "created_at", "created_by_kind",
        "created_by_ref", "created_by_meta_json",
    },
    "routing_dispositions": {
        "id", "evaluation_result_id", "decision", "rationale",
        "policy_snapshot_sha256", "canonical_sha256", "created_at",
        "created_by_kind", "created_by_ref", "created_by_meta_json",
    },
    "result_relations": {
        "id", "evaluation_result_id", "relation_kind", "target_kind",
        "target_ref", "canonical_sha256", "created_at",
        "created_by_kind", "created_by_ref", "created_by_meta_json",
    },
    "model_call_authorization_receipts": {
        "id", "action_snapshot_id", "plan_snapshot_id", "provider",
        "model", "context_sha256", "content_boundary_json",
        "egress_class", "cost_ceiling_usd", "retry_limit", "expires_at",
        "canonical_sha256", "created_at", "created_by_kind",
        "created_by_ref", "created_by_meta_json",
    },
    "cothink_items": {
        "id", "action_snapshot_id", "subtype", "purpose",
        "payload_json", "rationale", "delivery_state",
        "provenance_json", "canonical_sha256", "created_at",
        "created_by_kind", "created_by_ref", "created_by_meta_json",
    },
    "cothink_item_status_events": {
        "id", "cothink_item_id", "status", "reason", "canonical_sha256",
        "created_at", "created_by_kind", "created_by_ref",
        "created_by_meta_json",
    },
    "cowork_coordination_jobs": {
        "id", "document_id", "evaluation_run_id", "action_snapshot_id",
        "plan_snapshot_id", "role", "parent_job_id",
        "authorization_receipt_id", "context_sha256", "selection_json",
        "request_summary_json", "canonical_sha256", "created_at",
        "created_by_kind", "created_by_ref", "created_by_meta_json",
    },
    "cowork_coordination_status_events": {
        "id", "coordination_job_id", "status", "outcome_kind",
        "output_sha256", "error_code", "message",
        "consequence_refs_json", "canonical_sha256", "created_at",
        "created_by_kind", "created_by_ref", "created_by_meta_json",
    },
    "cowork_review_applications": {
        "id", "document_id", "applied_proposal_ids_json",
        "canonical_sha256", "committed_at", "created_by_kind",
        "created_by_ref", "created_by_meta_json",
    },
}


def test_schema_has_all_committed_tables_columns_indexes_and_triggers(
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
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        assert columns == expected

    index_rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
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
        "uq_documents_path",
        "idx_documents_ydoc_snapshot",
        "idx_document_spans_document",
        "idx_expressions_document_span",
        "idx_expressions_claim_ref",
        "idx_proposals_document",
        "idx_proposals_dedup",
        "idx_proposals_canonical",
        "idx_proposal_status_proposal_seq",
        "idx_doc_events_document",
        "idx_document_versions_document",
        "idx_document_versions_projection",
        "idx_document_versions_snapshot",
        "idx_document_provenance_document",
        "idx_document_provenance_version",
        "idx_document_provenance_span",
        "idx_document_provenance_supersedes",
        "uq_document_provenance_idempotency",
        "idx_cowork_bootstrap_state_expiry",
        "uq_cowork_bootstrap_live_path",
        "idx_cowork_materialization_state",
        "idx_cowork_sitting_state_expiry",
        "idx_cowork_sitting_document",
        "idx_cowork_reimport_state_expiry",
        "idx_cowork_reimport_document",
        "idx_cowork_retirement_state_expiry",
        "idx_cowork_retirement_document",
        "idx_cowork_coordination_document",
        "idx_cowork_coordination_run",
        "idx_cowork_coordination_parent",
        "idx_cowork_coordination_status_job",
        "idx_cowork_review_applications_document",
    } <= set(indexes)
    assert "WHERE status = 'confirmed'" in indexes["uq_claim_status_confirm_gesture"]

    triggers = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
    }
    assert len(triggers) == 79
    verify_tables = {
        "criterion_definition_versions",
        "check_definition_versions",
        "criterion_check_bindings",
        "criterion_activations",
        "action_snapshots",
        "evaluation_plan_snapshots",
        "evaluation_runs",
        "check_executions",
        "evaluation_results",
        "routing_dispositions",
        "result_relations",
        "model_call_authorization_receipts",
        "cothink_items",
        "cothink_item_status_events",
        "cowork_coordination_jobs",
        "cowork_coordination_status_events",
        "cowork_review_applications",
        "document_provenance_attestations",
    }
    assert {
        f"{table}_append_only_{operation}"
        for table in verify_tables
        for operation in ("update", "delete")
    } <= triggers
    assert not any(name.startswith("projections_") for name in triggers)
    assert not any(name.startswith("claims_current_") for name in triggers)
    assert truth_migrations.current_version(conn) == truth_migrations.SCHEMA_VERSION


def test_reopening_is_idempotent(tmp_path: Path):
    path = tmp_path / "store.db"
    conn = _connect(path)
    for _ in range(7):
        assert (
            truth_migrations.migrate(conn, path)
            == truth_migrations.SCHEMA_VERSION
        )
    assert (
        conn.execute("SELECT COUNT(*) FROM _migration_history").fetchone()[0]
        == truth_migrations.SCHEMA_VERSION
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger'"
        ).fetchone()[0]
        == 79
    )
    conn.close()


def test_v6_migration_backfills_open_cothink_status_append_only(
    tmp_path: Path,
    monkeypatch,
):
    path = tmp_path / "store.db"
    conn = _connect(path)
    current_runner = truth_migrations.TRUTH_MIGRATIONS
    v5_runner = truth_migrations._TruthMigrationRunner(
        "truth",
        migrations=list(current_runner.migrations[:5]),
    )
    monkeypatch.setattr(truth_migrations, "TRUTH_MIGRATIONS", v5_runner)
    assert truth_migrations.migrate(conn, path) == 5
    _insert_store_info(conn, 5)
    document_id = "d" * 32
    action_id = "a" * 32
    item_id = "c" * 32
    conn.execute(
        "INSERT INTO documents "
        "(id, path, title, document_class, content_sha256, "
        "ydoc_snapshot_sha256, created_at, created_by_kind, "
        "created_by_ref, meta_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            document_id,
            "docs/cothink-v5.md",
            "Co-think v5",
            "co_authored",
            "1" * 64,
            "2" * 64,
            NOW,
            "system",
            "migration-test",
            None,
        ),
    )
    conn.execute(
        "INSERT INTO ledger_records (record_type, record_key) "
        "VALUES ('document', ?)",
        (document_id,),
    )
    conn.execute(
        "INSERT INTO action_snapshots "
        "(id, document_id, document_version_id, ydoc_snapshot_sha256, "
        "structured_head_sha256, ydoc_generation_sha256, "
        "baseline_projection_sha256, projection_sha256, "
        "projection_blob_sha256, target_kind, target_selector_json, "
        "target_text_sha256, target_blob_sha256, context_boundary_json, "
        "allowed_change_ranges_json, egress_boundary_json, canonical_sha256, "
        "created_at, created_by_kind, created_by_ref, created_by_meta_json) "
        "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, 'document', ?, ?, ?, ?, ?, ?, "
        "?, ?, 'system', ?, NULL)",
        (
            action_id,
            document_id,
            "2" * 64,
            "3" * 64,
            "4" * 64,
            "1" * 64,
            "1" * 64,
            "1" * 64,
            '{"end":0,"kind":"document","start":0}',
            "1" * 64,
            "1" * 64,
            '{"kind":"action_target"}',
            '[{"end":0,"start":0}]',
            '{"class":"local_only"}',
            "5" * 64,
            NOW,
            "migration-test",
        ),
    )
    conn.execute(
        "INSERT INTO ledger_records (record_type, record_key) "
        "VALUES ('action_snapshot', ?)",
        (action_id,),
    )
    conn.execute(
        "INSERT INTO cothink_items "
        "(id, action_snapshot_id, subtype, purpose, payload_json, rationale, "
        "delivery_state, provenance_json, canonical_sha256, created_at, "
        "created_by_kind, created_by_ref, created_by_meta_json) "
        "VALUES (?, ?, 'question', 'Reflect', '{}', 'Useful friction', "
        "'delivered', '{}', ?, ?, 'system', 'migration-test', NULL)",
        (item_id, action_id, "6" * 64, NOW),
    )
    conn.execute(
        "INSERT INTO ledger_records (record_type, record_key) "
        "VALUES ('cothink_item', ?)",
        (item_id,),
    )
    conn.commit()

    monkeypatch.setattr(truth_migrations, "TRUTH_MIGRATIONS", current_runner)
    assert (
        truth_migrations.migrate(conn, path, snapshot=False)
        == truth_migrations.SCHEMA_VERSION
    )
    status = conn.execute(
        "SELECT * FROM cothink_item_status_events WHERE cothink_item_id = ?",
        (item_id,),
    ).fetchone()
    assert status["status"] == "open"
    assert status["reason"] is None
    assert status["created_by_kind"] == "system"
    assert status["created_by_ref"] == "truth-schema-v6"
    assert (
        status["created_by_meta_json"]
        == '{"basis":"pre_lifecycle_item_existence"}'
    )
    assert len(status["id"]) == 32
    assert len(status["canonical_sha256"]) == 64
    item_seq = conn.execute(
        "SELECT seq FROM ledger_records "
        "WHERE record_type = 'cothink_item' AND record_key = ?",
        (item_id,),
    ).fetchone()[0]
    status_seq = conn.execute(
        "SELECT seq FROM ledger_records "
        "WHERE record_type = 'cothink_item_status_event' AND record_key = ?",
        (status["id"],),
    ).fetchone()[0]
    assert status_seq > item_seq
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE cothink_item_status_events SET status = 'parked' "
            "WHERE id = ?",
            (status["id"],),
        )
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
        "UPDATE evidence_spans SET selector_json = ?, quote_exact = NULL, "
        "redacted_at = ? WHERE id = 'sp1'",
        (truth_migrations.REDACTED_SELECTOR_JSON, NOW),
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
    conn.execute("UPDATE gestures SET payload_excerpt = '[redacted]' WHERE id = 'g1'")
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

    assert tuple(
        conn.execute(
            "SELECT content, content_path FROM evidence WHERE id = 'e1'"
        ).fetchone()
    ) == (None, None)
    assert tuple(
        conn.execute(
            "SELECT proposition, canonical_sha256 FROM claims WHERE id = 'c1'"
        ).fetchone()
    ) == ("[redacted]", "hash-c1")
    assert tuple(
        conn.execute(
            "SELECT selector_json, quote_exact, span_sha256 "
            "FROM evidence_spans WHERE id = 'sp1'"
        ).fetchone()
    ) == (
        truth_migrations.REDACTED_SELECTOR_JSON,
        None,
        "hash-sp1",
    )
    assert tuple(
        conn.execute(
            "SELECT payload_excerpt, payload_sha256, consumed_at "
            "FROM gestures WHERE id = 'g1'"
        ).fetchone()
    ) == ("[redacted]", "hash-c1", NOW)
    assert (
        conn.execute(
            "SELECT resolved_by_ref FROM sweep_findings WHERE id = 'sf1'"
        ).fetchone()[0]
        == "user-1"
    )

    conn.execute("BEGIN")
    conn.execute(
        "UPDATE store_info SET schema_version = ? WHERE store_id = 'store-1'",
        (truth_migrations.SCHEMA_VERSION,),
    )
    assert (
        conn.execute("SELECT schema_version FROM store_info").fetchone()[0]
        == truth_migrations.SCHEMA_VERSION
    )
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
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM claims WHERE canonical_sha256 = 'same-hash'"
        ).fetchone()[0]
        == 2
    )
    index = {
        row["name"]: row["unique"] for row in conn.execute("PRAGMA index_list(claims)")
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


@pytest.mark.parametrize(
    "sql",
    (
        # The old span carve-out is no longer sufficient: quote context in
        # selector_json must be destroyed in the same one-way update.
        "UPDATE evidence_spans SET quote_exact = NULL, redacted_at = '"
        + NOW
        + "' WHERE id = 'sp1'",
        "UPDATE evidence_spans SET selector_json = '"
        + truth_migrations.REDACTED_SELECTOR_JSON
        + "' WHERE id = 'sp1'",
        "UPDATE evidence_spans SET selector_json = '[]', quote_exact = NULL, "
        "redacted_at = '" + NOW + "' WHERE id = 'sp1'",
        # A receipt cannot be erased before its bound subject is redacted.
        "UPDATE gestures SET payload_excerpt = '[redacted]' WHERE id = 'g1'",
    ),
)
def test_redaction_tombstone_carveouts_reject_partial_or_unbound_shapes(
    migrated_db,
    sql: str,
) -> None:
    conn, _ = migrated_db
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(sql)
    conn.rollback()


def test_gesture_tombstone_cannot_be_combined_with_other_sanctioned_mutation(
    migrated_db,
) -> None:
    conn, _ = migrated_db
    conn.execute(
        "UPDATE claims SET proposition = '[redacted]', structured_json = NULL, "
        "redacted_at = ? WHERE id = 'c1'",
        (NOW,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE gestures SET payload_excerpt = '[redacted]', consumed_at = ? "
            "WHERE id = 'g1'",
            (NOW,),
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
    monkeypatch.setattr(truth_migrations, "TRUTH_MIGRATIONS", _v1_runner())
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
    assert conn.execute("SELECT schema_version FROM store_info").fetchone()[0] == 1
    assert (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'rolled_back_marker'"
        ).fetchone()
        is None
    )
    assert conn.execute("SELECT COUNT(*) FROM _migration_history").fetchone()[0] == 1
    conn.close()


def test_snapshot_precedes_synthetic_v2_migration(tmp_path: Path, monkeypatch):
    path = tmp_path / "store.db"
    conn = _connect(path)
    monkeypatch.setattr(truth_migrations, "TRUTH_MIGRATIONS", _v1_runner())
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
    assert conn.execute("SELECT schema_version FROM store_info").fetchone()[0] == 2
    assert (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'migration_v2_marker'"
        ).fetchone()
        is not None
    )

    old = _connect(snapshot)
    assert old.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert truth_migrations.current_version(old) == 1
    assert old.execute("SELECT schema_version FROM store_info").fetchone()[0] == 1
    assert (
        old.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'migration_v2_marker'"
        ).fetchone()
        is None
    )
    old.close()
    conn.close()


def test_each_version_bump_gets_its_own_snapshot(tmp_path: Path, monkeypatch):
    path = tmp_path / "store.db"
    conn = _connect(path)
    monkeypatch.setattr(truth_migrations, "TRUTH_MIGRATIONS", _v1_runner())
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
    assert (
        old_v1.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'migration_v2_marker'"
        ).fetchone()
        is None
    )
    old_v1.close()

    old_v2 = _connect(pre_v2)
    assert truth_migrations.current_version(old_v2) == 2
    assert (
        old_v2.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'migration_v2_marker'"
        ).fetchone()
        is not None
    )
    assert (
        old_v2.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'migration_v3_marker'"
        ).fetchone()
        is None
    )
    old_v2.close()
    conn.close()


def test_newer_store_version_is_refused_before_snapshot(tmp_path: Path):
    path = tmp_path / "store.db"
    conn = _connect(path)
    future_version = truth_migrations.SCHEMA_VERSION + 1
    conn.execute(f"PRAGMA user_version = {future_version}")
    conn.commit()
    with pytest.raises(
        SchemaVersionTooNew,
        match=f"only knows up to v{truth_migrations.SCHEMA_VERSION}",
    ):
        truth_migrations.migrate(conn, path)
    assert not (tmp_path / f"store.pre-v{future_version}.db").exists()
    conn.close()


def test_partial_v0_schema_is_refused_not_stamped(tmp_path: Path):
    path = tmp_path / "store.db"
    conn = _connect(path)
    conn.execute("CREATE TABLE claims (id TEXT PRIMARY KEY)")
    conn.commit()
    with pytest.raises(MigrationError, match="unversioned partial schema"):
        truth_migrations.migrate(conn, path)
    assert truth_migrations.current_version(conn) == 0
    assert (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'store_info'"
        ).fetchone()
        is None
    )
    conn.close()


def test_store_info_and_pragma_version_mismatch_is_refused(
    tmp_path: Path,
    monkeypatch,
):
    path = tmp_path / "store.db"
    conn = _connect(path)
    monkeypatch.setattr(truth_migrations, "TRUTH_MIGRATIONS", _v1_runner())
    assert truth_migrations.migrate(conn, path) == 1
    _insert_store_info(conn)
    conn.commit()
    conn.execute("UPDATE store_info SET schema_version = 2")
    conn.commit()
    with pytest.raises(MigrationError, match="does not match"):
        truth_migrations.migrate(conn, path)
    conn.close()
