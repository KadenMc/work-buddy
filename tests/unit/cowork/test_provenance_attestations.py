"""Durable authorship and human-review provenance for Co-work content."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from work_buddy.cowork import bootstrap, provenance
from work_buddy.truth import documents
from work_buddy.truth.contracts import Actor, InvariantViolation
from work_buddy.truth.export import export_store, import_store
from work_buddy.truth.identity import sha256_bytes
from work_buddy.truth.queries import integrity_findings

from .conftest import HUMAN


class _EmptyRegistry:
    def paths_for_store_id(self, _store_id: str):
        return ()


def _attestation(
    *,
    authorship: str = "human",
    review: str = "reviewed",
    current_user_ref: str = HUMAN.ref or "",
    current_user_identity_status: str = "local_actor_ref",
) -> dict[str, object]:
    contributors: list[dict[str, str]] = (
        [
            {
                "kind": "current_user",
                "ref": current_user_ref,
                "identity_status": current_user_identity_status,
            }
        ]
        if authorship in {"human", "mixed"}
        else []
    )
    reviewers: list[dict[str, str]] = (
        [{"kind": "named_person", "display_name": "Dr. Rivera"}]
        if review == "reviewed"
        else []
    )
    return {
        "schema": provenance.INPUT_ATTESTATION_SCHEMA,
        "authorship": {
            "kind": authorship,
            "contributors": contributors,
        },
        "human_review": {
            "status": review,
            "reviewers": reviewers,
        },
    }


def test_current_user_binding_is_frozen_and_account_strength_is_server_trusted():
    account_actor = Actor(
        "human",
        "account:user-17",
        {"identity_status": "account_ref"},
    )
    normalized = provenance.normalize_attestation(
        _attestation(
            current_user_ref="account:user-17",
            current_user_identity_status="account_ref",
        ),
        actor=account_actor,
    )
    assert normalized["authorship"]["contributors"] == [
        {
            "kind": "human",
            "ref": "account:user-17",
            "identity_status": "account_ref",
        }
    ]
    assert normalized["human_review"]["reviewers"] == [
        {
            "kind": "human",
            "display_name": "Dr. Rivera",
            "identity_status": "claimed_name",
        }
    ]

    with pytest.raises(
        provenance.ProvenanceActorBindingError,
        match="acting user changed",
    ):
        provenance.normalize_attestation(
            _attestation(current_user_ref="account:someone-else"),
            actor=HUMAN,
        )
    with pytest.raises(
        provenance.ProvenanceActorBindingError,
        match="identity binding changed",
    ):
        provenance.normalize_attestation(
            _attestation(
                current_user_ref=HUMAN.ref or "",
                current_user_identity_status="account_ref",
            ),
            actor=HUMAN,
        )
    with pytest.raises(InvariantViolation, match=r"contributor\.ref is required"):
        provenance.normalize_attestation(
            {
                **_attestation(),
                "authorship": {
                    "kind": "human",
                    "contributors": [
                        {
                            "kind": "current_user",
                            "identity_status": "local_actor_ref",
                        }
                    ],
                },
            },
            actor=HUMAN,
        )


def test_file_import_rejects_a_current_user_binding_from_another_actor(
    store_ctx,
):
    store = store_ctx["store"]
    source = b"# Actor-bound import\n"
    target = store_ctx["root"] / "imports" / "actor-bound.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(source)

    with pytest.raises(bootstrap.BootstrapError) as rejected:
        bootstrap.prepare_bootstrap(
            store,
            metadata={
                "mode": "import",
                "path": "imports/actor-bound.md",
                "expected_file_sha256": sha256_bytes(source),
                "authorship_attestation": _attestation(
                    current_user_ref="another-dashboard-user",
                ),
                "idempotency_key": "actor-bound-import-0001",
            },
            source=None,
            actor=HUMAN,
        )

    assert rejected.value.code == "provenance_actor_changed"
    assert rejected.value.status == 409
    with store._read_connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM cowork_bootstrap_intents"
        ).fetchone()[0] == 0


def test_file_import_commit_revalidates_the_staged_identity_strength(
    store_ctx,
):
    store = store_ctx["store"]
    source = b"# Identity-strength-bound import\n"
    target = store_ctx["root"] / "imports" / "identity-strength-bound.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(source)
    intent, _ = bootstrap.prepare_bootstrap(
        store,
        metadata={
            "mode": "import",
            "path": "imports/identity-strength-bound.md",
            "expected_file_sha256": sha256_bytes(source),
            "authorship_attestation": _attestation(),
            "idempotency_key": "identity-strength-import-0001",
        },
        source=None,
        actor=HUMAN,
    )
    same_ref_authenticated_actor = Actor(
        "human",
        HUMAN.ref,
        {"identity_status": "account_ref"},
    )
    snapshot = b"identity-strength-snapshot"

    with pytest.raises(bootstrap.BootstrapError) as rejected:
        bootstrap.commit_bootstrap(
            store,
            bootstrap_id=intent.id,
            snapshot=snapshot,
            source_sha256=intent.source_sha256,
            snapshot_sha256=sha256_bytes(snapshot),
            ydoc_schema=bootstrap.YDOC_SCHEMA,
            actor=same_ref_authenticated_actor,
            projection=source,
            projection_sha256=sha256_bytes(source),
        )

    assert rejected.value.code == "provenance_actor_changed"
    assert rejected.value.status == 409
    assert bootstrap.get_intent(store, intent.id).state == "prepared"
    with store._read_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


def _import_ready(store_ctx, *, attestation=None):
    store = store_ctx["store"]
    source = b"# Imported\n\nA durable provenance target.\n"
    path = "imports/provenance.md"
    target = store_ctx["root"] / path
    target.parent.mkdir(parents=True)
    target.write_bytes(source)
    metadata = {
        "mode": "import",
        "path": path,
        "title": "Imported provenance",
        "expected_file_sha256": sha256_bytes(source),
        "idempotency_key": "provenance-import-0001",
    }
    if attestation is not None:
        metadata["authorship_attestation"] = attestation
    intent, _ = bootstrap.prepare_bootstrap(
        store,
        metadata=metadata,
        source=None,
        actor=HUMAN,
    )
    snapshot = b"opaque-provenance-snapshot"
    receipt = bootstrap.commit_bootstrap(
        store,
        bootstrap_id=intent.id,
        snapshot=snapshot,
        source_sha256=intent.source_sha256,
        snapshot_sha256=sha256_bytes(snapshot),
        ydoc_schema=bootstrap.YDOC_SCHEMA,
        actor=HUMAN,
        projection=source,
        projection_sha256=sha256_bytes(source),
    )
    document = documents.get_document(store, receipt["document_id"])
    version = documents.document_versions(store, document.id)[0]
    return document, version, intent, source


def test_import_records_independent_authorship_review_and_attester(store_ctx):
    store = store_ctx["store"]
    document, version, intent, source = _import_ready(
        store_ctx,
        attestation=_attestation(),
    )

    rows = store.list_document_provenance_attestations(document.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.target_kind == "document_version"
    assert row.document_version_id == version.id
    assert row.document_span_id is None
    assert row.target_structured_head_sha256 == version.structured_head_sha256
    assert row.authorship_kind == "human"
    assert json.loads(row.human_contributors_json) == [
        {
            "identity_status": "local_actor_ref",
            "kind": "human",
            "ref": HUMAN.ref,
        }
    ]
    assert row.review_status == "reviewed"
    assert json.loads(row.human_reviewers_json) == [
        {
            "display_name": "Dr. Rivera",
            "identity_status": "claimed_name",
            "kind": "human",
        }
    ]
    assert row.attested_by_kind == "human"
    assert row.attested_by_ref == HUMAN.ref
    assert row.source_kind == "file_import"
    assert json.loads(row.source_json) == {
        "format": "markdown",
        "kind": "file_import",
        "media_type": "text/markdown",
        "path": document.path,
        "sha256": sha256_bytes(source),
    }
    assert row.idempotency_key == f"bootstrap:{intent.id}"

    listed = provenance.list_attestations(store, document.id)
    assert listed[0]["attestation_id"] == row.id
    assert listed[0]["authorship"]["contributors"][0]["ref"] == HUMAN.ref
    assert listed[0]["human_review"]["reviewers"][0]["display_name"] == "Dr. Rivera"

    with store.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM doc_events "
                "WHERE kind = 'authorship_attested'"
            ).fetchone()[0]
            == 0
        )


def test_idempotency_and_append_only_supersession_keep_history(store_ctx):
    store = store_ctx["store"]
    document, version, intent, source = _import_ready(
        store_ctx,
        attestation=_attestation(),
    )
    original = store.list_document_provenance_attestations(document.id)[0]
    replay = provenance.record_document_attestation(
        store,
        document_id=document.id,
        document_version_id=version.id,
        attestation=_attestation(),
        source={
            "kind": "file_import",
            "format": "markdown",
            "media_type": "text/markdown",
            "path": document.path,
            "sha256": sha256_bytes(source),
        },
        actor=HUMAN,
        idempotency_key=f"bootstrap:{intent.id}",
    )
    assert replay == original

    with pytest.raises(InvariantViolation, match="different provenance"):
        provenance.record_document_attestation(
            store,
            document_id=document.id,
            document_version_id=version.id,
            attestation=_attestation(authorship="ai"),
            source={
                "kind": "file_import",
                "path": document.path,
                "sha256": sha256_bytes(source),
            },
            actor=HUMAN,
            idempotency_key=f"bootstrap:{intent.id}",
        )

    with pytest.raises(provenance.ProvenanceConflictError) as duplicate:
        provenance.record_document_attestation(
            store,
            document_id=document.id,
            document_version_id=version.id,
            attestation=_attestation(),
            source={
                "kind": "file_import",
                "format": "markdown",
                "media_type": "text/markdown",
                "path": document.path,
                "sha256": sha256_bytes(source),
            },
            actor=HUMAN,
            idempotency_key="provenance-duplicate-content-0001",
        )
    assert duplicate.value.code == "provenance_idempotency_conflict"
    assert duplicate.value.status == 409
    assert duplicate.value.retryable is False
    assert duplicate.value.details == {
        "existing_attestation_id": original.id,
    }
    assert len(store.list_document_provenance_attestations(document.id)) == 1

    replacement = provenance.record_document_attestation(
        store,
        document_id=document.id,
        document_version_id=version.id,
        attestation=_attestation(review="not_reviewed"),
        source={
            "kind": "file_import",
            "path": document.path,
            "sha256": sha256_bytes(source),
        },
        actor=HUMAN,
        idempotency_key="provenance-correction-0001",
        supersedes_id=original.id,
    )
    assert replacement.supersedes_id == original.id
    assert len(store.list_document_provenance_attestations(document.id)) == 2

    with store.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE document_provenance_attestations "
                "SET review_status = 'reviewed' WHERE id = ?",
                (replacement.id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "DELETE FROM document_provenance_attestations WHERE id = ?",
                (replacement.id,),
            )


def test_paste_attestation_targets_exact_span_and_rejects_a_stale_head(
    store_ctx,
):
    store = store_ctx["store"]
    document, version, _intent, _source = _import_ready(
        store_ctx,
        attestation=_attestation(),
    )
    event, span_id = provenance.record_span_attestation(
        store,
        document_id=document.id,
        exact="A durable provenance target.",
        prefix="# Imported\n\n",
        attestation=_attestation(authorship="ai", review="not_reviewed"),
        actor=HUMAN,
        idempotency_key="paste-provenance-0001",
        expected_structured_head_sha256=version.structured_head_sha256,
    )
    assert event.target_kind == "document_span"
    assert event.document_span_id == span_id
    assert event.document_version_id is None
    assert event.target_structured_head_sha256 == version.structured_head_sha256
    assert event.source_kind == "paste"

    with store.connect() as conn:
        prior_span_count = conn.execute(
            "SELECT COUNT(*) FROM document_spans"
        ).fetchone()[0]
    with pytest.raises(InvariantViolation, match="target changed"):
        provenance.record_span_attestation(
            store,
            document_id=document.id,
            exact="A durable provenance target.",
            attestation=_attestation(authorship="ai", review="not_reviewed"),
            actor=HUMAN,
            idempotency_key="paste-provenance-0002",
            expected_structured_head_sha256="0" * 64,
        )
    with store.connect() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM document_spans").fetchone()[0]
            == prior_span_count
        )


def test_paste_idempotency_key_is_bound_to_the_exact_selector(store_ctx):
    store = store_ctx["store"]
    document, version, _intent, _source = _import_ready(
        store_ctx,
        attestation=_attestation(),
    )
    original, original_span_id = provenance.record_span_attestation(
        store,
        document_id=document.id,
        exact="durable provenance",
        prefix="A ",
        suffix=" target.",
        attestation=_attestation(authorship="ai", review="not_reviewed"),
        actor=HUMAN,
        idempotency_key="paste-selector-bound-0001",
        expected_structured_head_sha256=version.structured_head_sha256,
    )

    replay, replay_span_id = provenance.record_span_attestation(
        store,
        document_id=document.id,
        exact="durable provenance",
        prefix="A ",
        suffix=" target.",
        attestation=_attestation(authorship="ai", review="not_reviewed"),
        actor=HUMAN,
        idempotency_key="paste-selector-bound-0001",
        expected_structured_head_sha256=version.structured_head_sha256,
    )
    assert replay == original
    assert replay_span_id == original_span_id

    with pytest.raises(InvariantViolation, match="different provenance span"):
        provenance.record_span_attestation(
            store,
            document_id=document.id,
            exact="Imported",
            prefix="# ",
            suffix="\n\nA",
            attestation=_attestation(authorship="ai", review="not_reviewed"),
            actor=HUMAN,
            idempotency_key="paste-selector-bound-0001",
            expected_structured_head_sha256=version.structured_head_sha256,
        )


def test_provenance_round_trips_and_integrity_detects_canonical_tampering(
    store_ctx,
    tmp_path: Path,
):
    store = store_ctx["store"]
    document, _version, _intent, _source = _import_ready(
        store_ctx,
        attestation=_attestation(authorship="ai", review="not_reviewed"),
    )
    assert [finding for finding in integrity_findings(store) if finding.severity == "error"] == []

    exported = export_store(store)
    target = tmp_path / "restored"
    target.mkdir()
    restored = import_store(
        exported.path,
        target,
        registry=_EmptyRegistry(),
    ).store
    restored_rows = restored.list_document_provenance_attestations(document.id)
    assert restored_rows == store.list_document_provenance_attestations(document.id)
    assert [finding for finding in integrity_findings(restored) if finding.severity == "error"] == []

    with restored.connect() as conn:
        conn.execute(
            "DROP TRIGGER document_provenance_attestations_append_only_update"
        )
        conn.execute(
            "UPDATE document_provenance_attestations "
            "SET canonical_sha256 = ? WHERE id = ?",
            ("0" * 64, restored_rows[0].id),
        )
    assert "document-provenance-canonical-mismatch" in {
        finding.code for finding in integrity_findings(restored)
    }
