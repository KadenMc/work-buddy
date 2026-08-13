"""Durable authorship and human-review provenance for Co-work content."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from work_buddy.cowork import bootstrap, provenance
from work_buddy.truth import documents, export as truth_export, ydoc_store
from work_buddy.truth.contracts import Actor, InvariantViolation
from work_buddy.truth.export import FORMAT_VERSION, export_store, import_store
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


def _other_document(store_ctx, *, suffix: str):
    store = store_ctx["store"]
    source = f"# Other provenance target {suffix}\n".encode()
    path = f"imports/other-provenance-{suffix}.md"
    (store_ctx["root"] / path).write_bytes(source)
    snapshot_sha256 = ydoc_store.write_snapshot(
        store,
        snapshot=f"opaque-other-provenance-{suffix}".encode(),
    )
    return documents.register_document(
        store,
        path=path,
        title=f"Other provenance target {suffix}",
        document_class="co_authored",
        content_sha256=sha256_bytes(source),
        ydoc_snapshot_sha256=snapshot_sha256,
        actor=HUMAN,
    )


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


@pytest.mark.parametrize(
    ("source_kind", "basis_kind"),
    [
        ("direct_entry", "user_attestation"),
        ("direct_entry", "automatic_short_text_attribution"),
        ("paste", "automatic_direct_entry_attribution"),
        ("legacy", "automatic_direct_entry_attribution"),
    ],
)
def test_span_attestation_rejects_unsupported_source_basis_pairs(
    store_ctx,
    source_kind,
    basis_kind,
):
    store = store_ctx["store"]
    document, version, _intent, _source = _import_ready(store_ctx)
    history_before = store.list_document_provenance_attestations(document.id)
    with pytest.raises(InvariantViolation, match="allowed provenance pair"):
        provenance.record_span_attestation(
            store,
            document_id=document.id,
            exact="durable provenance",
            prefix="A ",
            suffix=" target.",
            attestation=_attestation(authorship="human", review="not_applicable"),
            actor=HUMAN,
            idempotency_key=f"invalid-source-basis-{source_kind}-{basis_kind}",
            source={"kind": source_kind, "format": "plain_text"},
            basis_kind=basis_kind,
            expected_structured_head_sha256=version.structured_head_sha256,
        )
    assert store.list_document_provenance_attestations(document.id) == history_before
    with store.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM document_spans WHERE document_id = ?",
                (document.id,),
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("authorship", ["ai", "mixed"])
def test_human_review_supersedes_effective_ai_without_rewriting_source(
    store_ctx,
    authorship,
):
    store = store_ctx["store"]
    document, version, _intent, _source = _import_ready(store_ctx)
    original, span_id = provenance.record_span_attestation(
        store,
        document_id=document.id,
        exact="durable provenance",
        prefix="A ",
        suffix=" target.",
        attestation=_attestation(
            authorship=authorship,
            review="not_reviewed",
        ),
        actor=HUMAN,
        idempotency_key=f"review-origin-{authorship}-0001",
        basis_kind="user_attestation",
        expected_structured_head_sha256=version.structured_head_sha256,
    )

    reviewed = provenance.record_human_review(
        store,
        document_id=document.id,
        attestation_id=original.id,
        actor=HUMAN,
        idempotency_key=f"human-review-{authorship}-0001",
        expected_structured_head_sha256=version.structured_head_sha256,
    )
    replay = provenance.record_human_review(
        store,
        document_id=document.id,
        attestation_id=original.id,
        actor=HUMAN,
        idempotency_key=f"human-review-{authorship}-0001",
        expected_structured_head_sha256=version.structured_head_sha256,
    )

    assert replay == reviewed
    assert reviewed.document_span_id == span_id
    assert reviewed.target_structured_head_sha256 == original.target_structured_head_sha256
    assert reviewed.authorship_kind == original.authorship_kind == authorship
    assert reviewed.human_contributors_json == original.human_contributors_json
    assert reviewed.source_json == original.source_json
    assert reviewed.review_status == "reviewed"
    assert json.loads(reviewed.human_reviewers_json) == [
        {
            "identity_status": "local_actor_ref",
            "kind": "human",
            "ref": HUMAN.ref,
        }
    ]
    assert original.basis_kind == "user_attestation"
    assert reviewed.basis_kind == "user_attestation"
    assert reviewed.basis_ref == original.id
    assert reviewed.supersedes_id == original.id

    projection = provenance.project_attestations(
        store,
        document.id,
        current_structured_head_sha256=version.structured_head_sha256,
    )
    target = projection["spans"][0]
    assert target["resolution"] == "resolved"
    assert target["effective_attestation"]["attestation_id"] == reviewed.id
    assert target["history"][0]["basis"]["kind"] == "user_attestation"
    assert target["history"][1]["basis"] == {
        "kind": "user_attestation",
        "ref": original.id,
    }


def test_human_review_of_proposal_acceptance_round_trips(
    store_ctx,
    tmp_path: Path,
):
    store = store_ctx["store"]
    document, initial_version, _intent, _source = _import_ready(store_ctx)
    _document, version, _event = documents.commit_document_version(
        store,
        document_id=document.id,
        kind="materialized",
        projection_sha256=initial_version.projection_sha256,
        ydoc_snapshot_sha256=initial_version.ydoc_snapshot_sha256,
        structured_head_sha256=initial_version.structured_head_sha256,
        actor=HUMAN,
    )
    proposal_id = "bb" * 16
    original = provenance.record_document_attestation(
        store,
        document_id=document.id,
        document_version_id=version.id,
        attestation=_attestation(authorship="ai", review="not_reviewed"),
        source={"kind": "proposal_acceptance", "proposal_id": proposal_id},
        actor=HUMAN,
        idempotency_key="proposal-acceptance-origin-0001",
        basis_kind="proposal_acceptance",
        basis_ref=proposal_id,
    )

    reviewed = provenance.record_human_review(
        store,
        document_id=document.id,
        attestation_id=original.id,
        actor=HUMAN,
        idempotency_key="proposal-acceptance-review-0001",
        expected_structured_head_sha256=version.structured_head_sha256,
    )

    assert original.source_kind == "proposal_acceptance"
    assert original.basis_kind == "proposal_acceptance"
    assert reviewed.source_kind == "proposal_acceptance"
    assert reviewed.basis_kind == "user_attestation"
    assert reviewed.basis_ref == original.id
    assert reviewed.supersedes_id == original.id

    exported = export_store(store)
    target = tmp_path / "proposal-review-restored"
    target.mkdir()
    restored = import_store(
        exported.path,
        target,
        registry=_EmptyRegistry(),
    ).store
    restored_rows = restored.list_document_provenance_attestations(document.id)
    assert restored_rows == store.list_document_provenance_attestations(document.id)
    assert any(
        row.id == reviewed.id
        and row.source_kind == "proposal_acceptance"
        and row.basis_kind == "user_attestation"
        for row in restored_rows
    )


def test_human_review_rejects_non_ai_stale_and_already_superseded_targets(
    store_ctx,
):
    store = store_ctx["store"]
    document, version, _intent, _source = _import_ready(store_ctx)
    human, _ = provenance.record_span_attestation(
        store,
        document_id=document.id,
        exact="durable provenance",
        prefix="A ",
        suffix=" target.",
        attestation=_attestation(authorship="human", review="not_applicable"),
        actor=HUMAN,
        idempotency_key="human-review-ineligible-origin-0001",
        expected_structured_head_sha256=version.structured_head_sha256,
    )
    with pytest.raises(provenance.ProvenanceReviewError) as ineligible:
        provenance.record_human_review(
            store,
            document_id=document.id,
            attestation_id=human.id,
            actor=HUMAN,
            idempotency_key="human-review-ineligible-0001",
            expected_structured_head_sha256=version.structured_head_sha256,
        )
    assert ineligible.value.code == "provenance_review_ineligible"

    ai, _ = provenance.record_span_attestation(
        store,
        document_id=document.id,
        exact="Imported",
        prefix="# ",
        suffix="\n\nA",
        attestation=_attestation(authorship="ai", review="not_reviewed"),
        actor=HUMAN,
        idempotency_key="human-review-ai-origin-0001",
        expected_structured_head_sha256=version.structured_head_sha256,
    )
    other_source = b"# Other provenance target\n"
    other_path = "imports/other-provenance.md"
    (store_ctx["root"] / other_path).write_bytes(other_source)
    other_snapshot = b"opaque-other-provenance-snapshot"
    other_snapshot_sha256 = ydoc_store.write_snapshot(
        store,
        snapshot=other_snapshot,
    )
    other = documents.register_document(
        store,
        path=other_path,
        title="Other provenance target",
        document_class="co_authored",
        content_sha256=sha256_bytes(other_source),
        ydoc_snapshot_sha256=other_snapshot_sha256,
        actor=HUMAN,
    )
    with pytest.raises(provenance.ProvenanceReviewError) as cross_document:
        provenance.record_human_review(
            store,
            document_id=other.id,
            attestation_id=ai.id,
            actor=HUMAN,
            idempotency_key="human-review-ai-cross-doc-0001",
            expected_structured_head_sha256=version.structured_head_sha256,
        )
    assert cross_document.value.code == "provenance_review_target_mismatch"

    with pytest.raises(provenance.ProvenanceReviewError) as stale:
        provenance.record_human_review(
            store,
            document_id=document.id,
            attestation_id=ai.id,
            actor=HUMAN,
            idempotency_key="human-review-ai-stale-0001",
            expected_structured_head_sha256="0" * 64,
        )
    assert stale.value.code == "provenance_target_changed"

    reviewed = provenance.record_human_review(
        store,
        document_id=document.id,
        attestation_id=ai.id,
        actor=HUMAN,
        idempotency_key="human-review-ai-success-0001",
        expected_structured_head_sha256=version.structured_head_sha256,
    )
    with pytest.raises(provenance.ProvenanceReviewError) as superseded:
        provenance.record_human_review(
            store,
            document_id=document.id,
            attestation_id=ai.id,
            actor=HUMAN,
            idempotency_key="human-review-ai-second-0001",
            expected_structured_head_sha256=version.structured_head_sha256,
        )
    assert superseded.value.code == "provenance_review_conflict"
    assert reviewed.supersedes_id == ai.id

    documents.retire_document(
        store,
        document_id=document.id,
        actor=HUMAN,
    )
    with pytest.raises(provenance.ProvenanceReviewError) as retired:
        provenance.record_human_review(
            store,
            document_id=document.id,
            attestation_id=ai.id,
            actor=HUMAN,
            idempotency_key="human-review-ai-retired-0001",
            expected_structured_head_sha256=version.structured_head_sha256,
        )
    assert retired.value.code == "provenance_review_state_conflict"


@pytest.mark.parametrize(
    "damage",
    ["missing", "wrong_document", "malformed_selector", "quote_mismatch"],
)
def test_human_review_rejects_damaged_span_target_before_append(
    store_ctx,
    damage,
):
    store = store_ctx["store"]
    document, version, _intent, _source = _import_ready(store_ctx)
    original, span_id = provenance.record_span_attestation(
        store,
        document_id=document.id,
        exact="durable provenance",
        prefix="A ",
        suffix=" target.",
        attestation=_attestation(authorship="ai", review="not_reviewed"),
        actor=HUMAN,
        idempotency_key=f"review-damaged-{damage}-origin-0001",
        expected_structured_head_sha256=version.structured_head_sha256,
    )
    history_before = store.list_document_provenance_attestations(document.id)
    other = (
        _other_document(store_ctx, suffix=damage)
        if damage == "wrong_document"
        else None
    )

    with store.connect() as conn:
        if damage == "missing":
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("DROP TRIGGER document_spans_append_only_delete")
            conn.execute("DELETE FROM document_spans WHERE id = ?", (span_id,))
        else:
            conn.execute("DROP TRIGGER document_spans_append_only_update")
            if damage == "wrong_document":
                assert other is not None
                conn.execute(
                    "UPDATE document_spans SET document_id = ? WHERE id = ?",
                    (other.id, span_id),
                )
            elif damage == "malformed_selector":
                conn.execute(
                    "UPDATE document_spans SET selector_json = ? WHERE id = ?",
                    ("{not-json", span_id),
                )
            else:
                conn.execute(
                    "UPDATE document_spans SET quote_exact = ? WHERE id = ?",
                    ("different quote", span_id),
                )

    with pytest.raises(provenance.ProvenanceReviewError) as rejected:
        provenance.record_human_review(
            store,
            document_id=document.id,
            attestation_id=original.id,
            actor=HUMAN,
            idempotency_key=f"review-damaged-{damage}-command-0001",
            expected_structured_head_sha256=version.structured_head_sha256,
        )

    assert rejected.value.code == "provenance_review_target_mismatch"
    assert (
        store.list_document_provenance_attestations(document.id)
        == history_before
    )


def test_human_review_replay_revalidates_span_target_before_idempotent_success(
    store_ctx,
):
    store = store_ctx["store"]
    document, version, _intent, _source = _import_ready(store_ctx)
    original, span_id = provenance.record_span_attestation(
        store,
        document_id=document.id,
        exact="durable provenance",
        prefix="A ",
        suffix=" target.",
        attestation=_attestation(authorship="ai", review="not_reviewed"),
        actor=HUMAN,
        idempotency_key="review-replay-target-origin-0001",
        expected_structured_head_sha256=version.structured_head_sha256,
    )
    reviewed = provenance.record_human_review(
        store,
        document_id=document.id,
        attestation_id=original.id,
        actor=HUMAN,
        idempotency_key="review-replay-target-command-0001",
        expected_structured_head_sha256=version.structured_head_sha256,
    )
    history_before = store.list_document_provenance_attestations(document.id)
    with store.connect() as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DROP TRIGGER document_spans_append_only_delete")
        conn.execute("DELETE FROM document_spans WHERE id = ?", (span_id,))

    with pytest.raises(provenance.ProvenanceReviewError) as rejected:
        provenance.record_human_review(
            store,
            document_id=document.id,
            attestation_id=original.id,
            actor=HUMAN,
            idempotency_key="review-replay-target-command-0001",
            expected_structured_head_sha256=version.structured_head_sha256,
        )

    assert rejected.value.code == "provenance_review_target_mismatch"
    assert reviewed in history_before
    assert (
        store.list_document_provenance_attestations(document.id)
        == history_before
    )


def test_human_review_rejects_document_version_moved_to_another_document(
    store_ctx,
):
    store = store_ctx["store"]
    document, version, _intent, _source = _import_ready(
        store_ctx,
        attestation=_attestation(authorship="ai", review="not_reviewed"),
    )
    original = store.list_document_provenance_attestations(document.id)[0]
    other = _other_document(store_ctx, suffix="version-owner")
    with store.connect() as conn:
        conn.execute("DROP TRIGGER document_versions_append_only_update")
        conn.execute(
            "UPDATE document_versions SET document_id = ? WHERE id = ?",
            (other.id, version.id),
        )

    with pytest.raises(provenance.ProvenanceReviewError) as rejected:
        provenance.record_human_review(
            store,
            document_id=document.id,
            attestation_id=original.id,
            actor=HUMAN,
            idempotency_key="review-version-owner-command-0001",
            expected_structured_head_sha256=version.structured_head_sha256,
        )

    assert rejected.value.code == "provenance_review_target_mismatch"
    assert [
        row.id
        for row in store.list_document_provenance_attestations(document.id)
    ] == [original.id]


def test_effective_projection_surfaces_peer_conflicts_without_last_write_wins(
    store_ctx,
):
    store = store_ctx["store"]
    document, version, _intent, source = _import_ready(store_ctx)
    source_view = {
        "kind": "file_import",
        "path": document.path,
        "sha256": sha256_bytes(source),
    }
    first = provenance.record_document_attestation(
        store,
        document_id=document.id,
        document_version_id=version.id,
        attestation=_attestation(authorship="ai", review="not_reviewed"),
        source=source_view,
        actor=HUMAN,
        idempotency_key="projection-peer-ai-0001",
    )
    second = provenance.record_document_attestation(
        store,
        document_id=document.id,
        document_version_id=version.id,
        attestation=_attestation(authorship="human", review="not_applicable"),
        source=source_view,
        actor=HUMAN,
        idempotency_key="projection-peer-human-0001",
    )

    projection = provenance.project_attestations(
        store,
        document.id,
        current_structured_head_sha256=version.structured_head_sha256,
    )

    default = projection["document_default"]
    assert default["resolution"] == "conflicted"
    assert default["effective_attestation"] is None
    effective_ids = {
        item["attestation_id"] for item in default["effective_attestations"]
    }
    assert {first.id, second.id} <= effective_ids
    assert projection["summary"]["conflicted_count"] == 1


def test_effective_projection_keeps_orphaned_span_targets_inspectable(
    store_ctx,
):
    store = store_ctx["store"]
    document, version, _intent, _source = _import_ready(store_ctx)
    orphaned, orphaned_span_id = provenance.record_span_attestation(
        store,
        document_id=document.id,
        exact="Imported",
        prefix="# ",
        suffix="\n\nA",
        attestation=_attestation(authorship="ai", review="not_reviewed"),
        actor=HUMAN,
        idempotency_key="projection-orphaned-span-0001",
        expected_structured_head_sha256=version.structured_head_sha256,
    )
    _extant, extant_span_id = provenance.record_span_attestation(
        store,
        document_id=document.id,
        exact="durable provenance",
        prefix="A ",
        suffix=" target.",
        attestation=_attestation(authorship="ai", review="not_reviewed"),
        actor=HUMAN,
        idempotency_key="projection-extant-span-0001",
        expected_structured_head_sha256=version.structured_head_sha256,
    )

    # Simulate a damaged or legacy store whose attestation survived the span
    # row. Normal writes cannot create this state because the FK and append-only
    # trigger are enforced.
    with store.connect() as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DROP TRIGGER document_spans_append_only_delete")
        conn.execute(
            "DELETE FROM document_spans WHERE id = ?",
            (orphaned_span_id,),
        )

    projection = provenance.project_attestations(
        store,
        document.id,
        current_structured_head_sha256=version.structured_head_sha256,
    )

    assert [
        item["target"]["document_span_id"] for item in projection["spans"]
    ] == [extant_span_id, orphaned_span_id]
    broken = projection["spans"][-1]
    assert broken["projection_id"] == f"document_span:{orphaned_span_id}"
    assert broken["target"]["currentness"] == "unavailable"
    assert broken["resolution"] == "conflicted"
    assert broken["review_eligibility"] == "conflicted"
    assert broken["span"] is None
    assert broken["effective_attestation"] is None
    assert broken["effective_attestations"][0]["attestation_id"] == orphaned.id
    assert broken["issue"]["code"] == "missing_span_target"
    assert projection["summary"]["conflicted_count"] == 1
    assert projection["summary"]["stale_count"] == 1


def test_effective_projection_keeps_malformed_span_selectors_inspectable(
    store_ctx,
):
    store = store_ctx["store"]
    document, version, _intent, _source = _import_ready(store_ctx)
    attestation, span_id = provenance.record_span_attestation(
        store,
        document_id=document.id,
        exact="durable provenance",
        prefix="A ",
        suffix=" target.",
        attestation=_attestation(authorship="ai", review="not_reviewed"),
        actor=HUMAN,
        idempotency_key="projection-malformed-selector-0001",
        expected_structured_head_sha256=version.structured_head_sha256,
    )

    # Simulate a damaged or legacy selector. The target history must remain
    # visible, but an invalid anchor cannot be called current or actionable.
    with store.connect() as conn:
        conn.execute("DROP TRIGGER document_spans_append_only_update")
        conn.execute(
            "UPDATE document_spans SET selector_json = ? WHERE id = ?",
            ("{not-json", span_id),
        )

    projection = provenance.project_attestations(
        store,
        document.id,
        current_structured_head_sha256=version.structured_head_sha256,
    )

    assert len(projection["spans"]) == 1
    broken = projection["spans"][0]
    assert broken["projection_id"] == f"document_span:{span_id}"
    assert broken["target"]["currentness"] == "unavailable"
    assert broken["resolution"] == "conflicted"
    assert broken["review_eligibility"] == "conflicted"
    assert broken["span"] is None
    assert broken["effective_attestation"] is None
    assert broken["effective_attestations"][0]["attestation_id"] == (
        attestation.id
    )
    assert broken["history"][0]["attestation_id"] == attestation.id
    assert broken["issue"]["code"] == "invalid_span_selector"
    assert projection["summary"]["current_span_count"] == 0
    assert projection["summary"]["conflicted_count"] == 1
    assert projection["summary"]["stale_count"] == 1


def test_effective_projection_mixed_target_heads_are_order_independent(
    store_ctx,
    monkeypatch,
):
    store = store_ctx["store"]
    document, version, _intent, _source = _import_ready(store_ctx)
    original, span_id = provenance.record_span_attestation(
        store,
        document_id=document.id,
        exact="durable provenance",
        prefix="A ",
        suffix=" target.",
        attestation=_attestation(authorship="ai", review="not_reviewed"),
        actor=HUMAN,
        idempotency_key="projection-mixed-head-origin-0001",
        expected_structured_head_sha256=version.structured_head_sha256,
    )
    successor = provenance.record_human_review(
        store,
        document_id=document.id,
        attestation_id=original.id,
        actor=HUMAN,
        idempotency_key="projection-mixed-head-review-0001",
        expected_structured_head_sha256=version.structured_head_sha256,
    )
    conflicting_head = (
        "0" * 64
        if version.structured_head_sha256 != "0" * 64
        else "1" * 64
    )

    # Normal writes preserve the frozen head across supersession. Simulate a
    # damaged legacy history in which the same stable target spans two heads.
    with store.connect() as conn:
        conn.execute(
            "DROP TRIGGER document_provenance_attestations_append_only_update"
        )
        conn.execute(
            "UPDATE document_provenance_attestations "
            "SET target_structured_head_sha256 = ? WHERE id = ?",
            (conflicting_head, successor.id),
        )

    projections = [
        provenance.project_attestations(
            store,
            document.id,
            current_structured_head_sha256=version.structured_head_sha256,
        )
    ]
    original_list = type(store).list_document_provenance_attestations

    def _reversed_attestations(self, document_id, *, conn=None):
        rows = original_list(self, document_id, conn=conn)
        return tuple(reversed(rows))

    # Reversing append-order delivery must not let the first row decide the
    # target head, currentness, eligibility, or summary.
    monkeypatch.setattr(
        type(store),
        "list_document_provenance_attestations",
        _reversed_attestations,
    )
    projections.append(
        provenance.project_attestations(
            store,
            document.id,
            current_structured_head_sha256=version.structured_head_sha256,
        )
    )

    for projection in projections:
        assert len(projection["spans"]) == 1
        conflicted = projection["spans"][0]
        assert conflicted["projection_id"] == f"document_span:{span_id}"
        assert conflicted["target"]["structured_head_sha256"] == min(
            conflicting_head,
            version.structured_head_sha256,
        )
        assert conflicted["target"]["currentness"] == "unavailable"
        assert conflicted["resolution"] == "conflicted"
        assert conflicted["review_eligibility"] == "conflicted"
        assert conflicted["effective_attestation"] is None
        assert projection["summary"]["current_span_count"] == 0
        assert projection["summary"]["stale_count"] == 1
        assert projection["summary"]["conflicted_count"] == 1


def test_provenance_round_trips_and_integrity_detects_canonical_tampering(
    store_ctx,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    store = store_ctx["store"]
    document, version, _intent, _source = _import_ready(
        store_ctx,
        attestation=_attestation(authorship="ai", review="not_reviewed"),
    )
    direct, _span_id = provenance.record_span_attestation(
        store,
        document_id=document.id,
        exact="durable provenance",
        prefix="A ",
        suffix=" target.",
        attestation=_attestation(authorship="human", review="not_applicable"),
        actor=HUMAN,
        idempotency_key="direct-entry-round-trip-0001",
        source={"kind": "direct_entry", "format": "plain_text"},
        basis_kind="automatic_direct_entry_attribution",
        expected_structured_head_sha256=version.structured_head_sha256,
    )
    assert direct.source_kind == "direct_entry"
    assert direct.basis_kind == "automatic_direct_entry_attribution"
    assert [finding for finding in integrity_findings(store) if finding.severity == "error"] == []

    exported = export_store(store)
    header = json.loads(exported.path.read_text(encoding="utf-8").splitlines()[0])
    assert header["format_version"] == FORMAT_VERSION == 10
    v9_target = tmp_path / "v9-reader"
    v9_target.mkdir()
    with monkeypatch.context() as v9_reader:
        v9_reader.setattr(truth_export, "FORMAT_VERSION", 9)
        with pytest.raises(
            truth_export.TruthImportError,
            match="newer than supported v9",
        ):
            import_store(
                exported.path,
                v9_target,
                registry=_EmptyRegistry(),
            )
    target = tmp_path / "restored"
    target.mkdir()
    imported = import_store(
        exported.path,
        target,
        registry=_EmptyRegistry(),
    )
    assert imported.source_format_version == 10
    restored = imported.store
    restored_rows = restored.list_document_provenance_attestations(document.id)
    assert restored_rows == store.list_document_provenance_attestations(document.id)
    assert any(
        row.source_kind == "direct_entry"
        and row.basis_kind == "automatic_direct_entry_attribution"
        for row in restored_rows
    )
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
