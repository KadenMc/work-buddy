"""Portable Truth persistence invariants for Co-work Verify and Co-think."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from work_buddy.cowork.verify import (
    ActionTarget,
    VerifyInvariantViolation,
    cothink_items,
    create_action_snapshot,
    create_terminology_plan,
    record_cothink_item,
    record_model_call_authorization,
    record_result_relation,
    record_routing_disposition,
    run_terminology_exact_match,
    seed_terminology_exact_match,
    surfaced_results,
)
from work_buddy.truth import documents, ydoc_store
from work_buddy.truth.contracts import Actor
from work_buddy.truth.export import FORMAT_VERSION, export_store, import_store
from work_buddy.truth.identity import sha256_bytes

from .conftest import HUMAN, NOW


SYSTEM = Actor("system", "verify-test")
LATER = "2026-07-17T13:00:00.000+00:00"
BODY = (
    "# Throwaway terminology fixture\n\n"
    "This paragraph still uses Co-work scope even though document target "
    "is preferred.\n"
)


class _EmptyRegistry:
    def paths_for_store_id(self, _store_id: str):
        return ()


def _ready_document(store_ctx, body: str = BODY):
    store = store_ctx["store"]
    path = "docs/throwaway-verify.md"
    file_path = store_ctx["root"] / path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    projection = body.encode("utf-8")
    file_path.write_bytes(projection)
    snapshot = b"YDOC-VERIFY-THROWAWAY:" + sha256_bytes(projection).encode("ascii")
    snapshot_sha256 = ydoc_store.write_snapshot(store, snapshot=snapshot)
    document = documents.register_document(
        store,
        path=path,
        title="Throwaway Verify fixture",
        document_class="co_authored",
        content_sha256=sha256_bytes(projection),
        ydoc_snapshot_sha256=snapshot_sha256,
        actor=HUMAN,
        at=NOW,
    )
    head = ydoc_store.current_structured_head(
        store,
        document_id=document.id,
        snapshot_sha256=snapshot_sha256,
    )
    generation = documents.current_ydoc_generation(store, document.id)
    return document, projection, snapshot_sha256, head, generation


def _capture(store_ctx, *, target=None):
    document, projection, snapshot, head, generation = _ready_document(store_ctx)
    action = create_action_snapshot(
        store_ctx["store"],
        document_id=document.id,
        projection=projection,
        expected_snapshot_sha256=snapshot,
        expected_structured_head_sha256=head,
        expected_ydoc_generation_sha256=generation,
        expected_projection_sha256=sha256_bytes(projection),
        target=target,
        actor=HUMAN,
        at=NOW,
    )
    return document, projection, action


def test_seeded_terminology_contract_is_idempotent_and_append_only(store_ctx):
    store = store_ctx["store"]
    first = seed_terminology_exact_match(store, actor=SYSTEM, at=NOW)
    second = seed_terminology_exact_match(store, actor=SYSTEM, at=LATER)

    assert second.criterion == first.criterion
    assert second.check == first.check
    assert second.binding == first.binding
    assert second.activation == first.activation

    with store.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM criterion_definition_versions"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM check_definition_versions"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM criterion_check_bindings"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM criterion_activations"
        ).fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE criterion_activations SET is_enabled = 0 WHERE id = ?",
                (first.activation.id,),
            )
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "DELETE FROM check_definition_versions WHERE id = ?",
                (first.check.id,),
            )


def test_action_snapshot_validates_exact_state_hashes_and_target_containment(
    store_ctx,
):
    store = store_ctx["store"]
    document, projection, snapshot, head, generation = _ready_document(store_ctx)
    action = create_action_snapshot(
        store,
        document_id=document.id,
        projection=projection,
        expected_snapshot_sha256=snapshot,
        expected_structured_head_sha256=head,
        expected_ydoc_generation_sha256=generation,
        expected_projection_sha256=sha256_bytes(projection),
        target=ActionTarget.text_quote("Co-work scope"),
        actor=HUMAN,
        at=NOW,
    )
    assert action.target_kind == "text_quote"
    assert (
        store.resolve_blob_path(f"blobs/{action.projection_blob_sha256}").read_bytes()
        == projection
    )
    assert (
        store.resolve_blob_path(f"blobs/{action.target_blob_sha256}").read_text(
            encoding="utf-8"
        )
        == "Co-work scope"
    )
    repeated = create_action_snapshot(
        store,
        document_id=document.id,
        projection=projection,
        expected_snapshot_sha256=snapshot,
        expected_structured_head_sha256=head,
        expected_ydoc_generation_sha256=generation,
        expected_projection_sha256=sha256_bytes(projection),
        target=ActionTarget.text_quote("Co-work scope"),
        actor=HUMAN,
        at=LATER,
    )
    assert repeated.id == action.id

    with pytest.raises(VerifyInvariantViolation, match="structured head changed"):
        create_action_snapshot(
            store,
            document_id=document.id,
            projection=projection,
            expected_snapshot_sha256=snapshot,
            expected_structured_head_sha256="0" * 64,
            expected_ydoc_generation_sha256=generation,
            expected_projection_sha256=sha256_bytes(projection),
            actor=HUMAN,
        )
    with pytest.raises(VerifyInvariantViolation, match="contained"):
        create_action_snapshot(
            store,
            document_id=document.id,
            projection=projection,
            expected_snapshot_sha256=snapshot,
            expected_structured_head_sha256=head,
            expected_ydoc_generation_sha256=generation,
            expected_projection_sha256=sha256_bytes(projection),
            target=ActionTarget.text_quote("Co-work scope"),
            allowed_change_ranges=[{"start": 0, "end": len(projection)}],
            actor=HUMAN,
        )


def test_raw_result_requires_latest_surfacing_disposition(store_ctx):
    store = store_ctx["store"]
    document, _, action = _capture(store_ctx)
    evaluation = run_terminology_exact_match(
        store,
        action_snapshot_id=action.id,
        actor=SYSTEM,
        at=NOW,
    )
    assert len(evaluation.results) == 1
    assert surfaced_results(store, document_id=document.id) == ()

    finding = evaluation.results[0]
    with pytest.raises(VerifyInvariantViolation, match="ProposalRecord does not exist"):
        record_result_relation(
            store,
            evaluation_result_id=finding.id,
            relation_kind="related",
            target_kind="proposal",
            target_ref="0" * 32,
            actor=SYSTEM,
            at=NOW,
        )
    surface = record_routing_disposition(
        store,
        evaluation_result_id=finding.id,
        decision="surface",
        rationale="The whole-document coordinator found this relevant.",
        actor=SYSTEM,
        at=NOW,
    )
    projected = surfaced_results(store, document_id=document.id)
    assert len(projected) == 1
    assert projected[0]["id"] == finding.id
    assert projected[0]["disposition"]["id"] == surface.id

    record_routing_disposition(
        store,
        evaluation_result_id=finding.id,
        decision="suppress",
        rationale="A later whole-document decision found the term intentional.",
        actor=SYSTEM,
        at=LATER,
    )
    assert surfaced_results(store, document_id=document.id) == ()
    repeated = run_terminology_exact_match(
        store,
        action_snapshot_id=action.id,
        actor=SYSTEM,
        at=LATER,
    )
    assert repeated.run.id == evaluation.run.id
    assert repeated.results == evaluation.results


def test_portable_round_trip_preserves_verify_cothink_and_action_blobs(
    store_ctx,
    tmp_path: Path,
):
    store = store_ctx["store"]
    document, _, action = _capture(store_ctx)
    evaluation = run_terminology_exact_match(
        store,
        action_snapshot_id=action.id,
        actor=SYSTEM,
        at=NOW,
    )
    finding = evaluation.results[0]
    record_routing_disposition(
        store,
        evaluation_result_id=finding.id,
        decision="surface",
        rationale="Coordinator admitted the exact terminology result.",
        actor=SYSTEM,
        at=NOW,
    )
    plan = create_terminology_plan(
        store,
        action_snapshot_id=action.id,
        actor=SYSTEM,
        at=NOW,
    )
    authorization = record_model_call_authorization(
        store,
        action_snapshot_id=action.id,
        plan_snapshot_id=plan.id,
        provider="local",
        model="deterministic-test",
        context_sha256=action.target_text_sha256,
        content_boundary={"action_snapshot_id": action.id},
        egress_class="local_only",
        cost_ceiling_usd=0,
        retry_limit=0,
        expires_at=LATER,
        actor=HUMAN,
        at=NOW,
    )
    item = record_cothink_item(
        store,
        action_snapshot_id=action.id,
        subtype="alternative_perspective",
        purpose="Invite another perspective",
        payload={"text": "The local term may be useful if explicitly defined."},
        rationale="Offer a non-evidential alternative without changing the document.",
        provenance={"kind": "deterministic_fixture"},
        actor=SYSTEM,
        at=NOW,
    )
    relation = record_result_relation(
        store,
        evaluation_result_id=finding.id,
        relation_kind="related",
        target_kind="cothink_item",
        target_ref=item.id,
        actor=SYSTEM,
        at=NOW,
    )

    exported = export_store(store, tmp_path / "verify.jsonl")
    header = exported.path.read_text(encoding="utf-8").splitlines()[0]
    assert f'"format_version":{FORMAT_VERSION}' in header
    target = tmp_path / "import-target"
    target.mkdir()
    restored = import_store(
        exported.path,
        target,
        registry=_EmptyRegistry(),
    ).store

    restored_results = surfaced_results(restored, document_id=document.id)
    assert [row["id"] for row in restored_results] == [finding.id]
    assert [row["id"] for row in cothink_items(restored)] == [item.id]
    assert restored.resolve_blob_path(
        f"blobs/{action.projection_blob_sha256}"
    ).read_bytes() == store.resolve_blob_path(
        f"blobs/{action.projection_blob_sha256}"
    ).read_bytes()
    with restored.connect() as conn:
        assert conn.execute(
            "SELECT id FROM model_call_authorization_receipts"
        ).fetchone()[0] == authorization.id
        assert conn.execute(
            "SELECT id FROM result_relations"
        ).fetchone()[0] == relation.id
