"""Deterministic recovery export tests for targeted truth stores."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from work_buddy.cowork.truth_activation import (
    WORKING_DOCUMENT_CONTRACT,
    provision_document_policy,
    resolve_document_truth_policy,
)
from work_buddy.cowork.verify import (
    instruction_model_check_defaults,
    terminology_exact_match_defaults,
)
from work_buddy.truth import documents, export as truth_export
from work_buddy.truth import migrations as truth_migrations
from work_buddy.truth.anchors import CompositeSelector
from work_buddy.truth.contracts import Actor
from work_buddy.truth.export import (
    FORMAT_NAME,
    FORMAT_VERSION,
    StoreIdentityCollision,
    TruthExportError,
    TruthImportError,
    export_store,
    import_store,
)
from work_buddy.truth.identity import canonical_json, new_id, sha256_bytes, truth_uri
from work_buddy.truth.lifecycle import TruthLifecycle
from work_buddy.truth.queries import integrity_findings
from work_buddy.truth.redact import TruthRedactor
from work_buddy.truth.store import (
    ClaimRecord,
    GestureRecord,
    PostCommitHookError,
    TruthStore,
)


NOW = "2026-07-14T16:00:00.000+00:00"
LATER = "2026-07-14T16:01:00.000+00:00"
AFTER = "2026-07-14T16:02:00.000+00:00"
FINAL = "2026-07-14T16:03:00.000+00:00"
HUMAN = Actor("human", "user-1")
SYSTEM = Actor("system", "truth-export-test")

EVIDENCE_ID = "01" * 16
BLOB_EVIDENCE_ID = "02" * 16
SHARED_EVIDENCE_ID = "03" * 16
SPAN_ID = "04" * 16
CLAIM_ID = "05" * 16
DERIVED_CLAIM_ID = "06" * 16
SUPPORT_LINK_ID = "07" * 16
DERIVATION_ID = "08" * 16
PROPOSED_EVENT_ID = "09" * 16
DERIVED_EVENT_ID = "0a" * 16
GESTURE_ID = "0b" * 16
REDACTION_ID = "0c" * 16
SWEEP_ID = "0d" * 16
FINDING_ID = "0e" * 16


def _profile(store_id: str | None = None) -> dict[str, Any]:
    return {
        "store_id": store_id or new_id(),
        "profile": "test",
        "title": "Portable truth store",
        "allowed_claim_kinds": ["fact", "preference"],
        "required_fields": {},
        "gate": {
            "rejected_content": "redact",
            "confirmation_surfaces": ["dashboard", "cli"],
            "block_materialize_on_flags": False,
        },
        "projection": "resident",
        "export_committed": True,
        "extensions": {"privacy_scope": "private-test"},
    }


@dataclass
class FakeRegistry:
    paths: dict[str, list[Path]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def paths_for_store_id(self, store_id: str):
        self.calls.append(store_id)
        return tuple(self.paths.get(store_id, ()))


def _create_store(
    root: Path,
    *,
    store_id: str | None = None,
    inline_content_bytes: int = 64,
) -> TruthStore:
    root.mkdir(exist_ok=True)
    return TruthStore.create(
        root,
        _profile(store_id),
        inline_content_bytes=inline_content_bytes,
    )


def _populate_full_store(root: Path) -> TruthStore:
    store = _create_store(root, store_id="10" * 16, inline_content_bytes=64)
    text = "Alpha βeta supports the claim."
    evidence = store.capture_evidence(
        kind="document",
        source_locator="file:///source.md",
        actor=HUMAN,
        acquisition_method="paste",
        content=text,
        record_id=EVIDENCE_ID,
        acquired_at=NOW,
        created_at=NOW,
    )
    binary = bytes(range(256))
    first_blob = store.capture_evidence(
        kind="artifact",
        source_locator="file:///artifact.bin",
        actor=HUMAN,
        acquisition_method="paste",
        content=binary,
        media_type="application/octet-stream",
        record_id=BLOB_EVIDENCE_ID,
        acquired_at=NOW,
        created_at=NOW,
    )
    shared_blob = store.capture_evidence(
        kind="artifact",
        source_locator="file:///artifact-copy.bin",
        actor=HUMAN,
        acquisition_method="paste",
        content=binary,
        media_type="application/octet-stream",
        record_id=SHARED_EVIDENCE_ID,
        acquired_at=NOW,
        created_at=NOW,
    )
    assert first_blob.content_path == shared_blob.content_path

    span = store.mark_span(
        evidence_id=evidence.id,
        selector=CompositeSelector(
            exact="Alpha βeta",
            prefix="",
            suffix=" supports",
            start=0,
            end=10,
        ),
        actor=HUMAN,
        record_id=SPAN_ID,
        created_at=NOW,
    )
    claim = store.propose_claim(
        proposition="Alpha beta is supported",
        claim_kind="fact",
        actor=HUMAN,
        record_id=CLAIM_ID,
        status_event_id=PROPOSED_EVENT_ID,
        created_at=NOW,
        status_at=NOW,
    ).claim
    derived = store.propose_claim(
        proposition="The derived result follows",
        claim_kind="fact",
        actor=HUMAN,
        record_id=DERIVED_CLAIM_ID,
        status_event_id=DERIVED_EVENT_ID,
        created_at=NOW,
        status_at=NOW,
    ).claim
    link = store.add_link(
        from_claim_id=claim.id,
        link_type="supports_span",
        to_kind="evidence_span",
        to_ref=span.id,
        actor=HUMAN,
        record_id=SUPPORT_LINK_ID,
        created_at=NOW,
    )
    store.add_derivation(
        claim_id=derived.id,
        method="deduction",
        premises=[claim.id],
        actor=HUMAN,
        record_id=DERIVATION_ID,
        created_at=NOW,
    )
    store.retract_link(
        link_id=link.id,
        actor=HUMAN,
        reason="source mapping replaced",
        at=LATER,
    )

    lifecycle = TruthLifecycle(store)
    gesture = lifecycle.mint_gesture(
        subject_ref=derived.id,
        actor=HUMAN,
        surface="dashboard",
        kind="redact",
        displayed_payload_sha256=derived.canonical_sha256,
        gesture_id=GESTURE_ID,
        at=LATER,
    )
    TruthRedactor(store, lifecycle=lifecycle).redact(
        subject_kind="claim",
        subject_ref=derived.id,
        actor=HUMAN,
        reason="privacy",
        basis_kind="gesture",
        basis_ref=gesture.id,
        event_id=REDACTION_ID,
        at=LATER,
    )
    with store.write_transaction() as conn:
        conn.execute(
            "INSERT INTO sweeps (id, kind, at, params_json) "
            "VALUES (?, 'integrity', ?, ?)",
            (SWEEP_ID, LATER, canonical_json({"scope": "store"})),
        )
        store._insert_ledger_record_locked(conn, "sweep", SWEEP_ID)
        conn.execute(
            "INSERT INTO sweep_findings "
            "(id, sweep_id, subject_kind, subject_ref, finding, resolved_at, "
            "resolved_by_ref) VALUES (?, ?, 'claim', ?, 'needs_review', ?, 'user-1')",
            (FINDING_ID, SWEEP_ID, claim.id, LATER),
        )
        store._insert_ledger_record_locked(conn, "sweep_finding", FINDING_ID)

        conn.execute(
            "INSERT INTO projections "
            "(id, path, rendered_at, content_sha256, manifest_json, health, "
            "health_reason) VALUES (?, 'canon.md', ?, ?, '[]', 'clean', NULL)",
            ("0f" * 16, LATER, "11" * 32),
        )
        conn.execute(
            "INSERT INTO claims_current "
            "(claim_id, status, status_seq, effective_valid_from, "
            "effective_valid_to, health, health_reason, rebuilt_at) "
            "VALUES (?, 'proposed', 1, NULL, NULL, 'clean', NULL, ?)",
            (claim.id, LATER),
        )
    return store


def _objects(payload: bytes) -> list[dict[str, Any]]:
    return [json.loads(line) for line in payload.decode("utf-8").splitlines()]


def _canonical_line(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _v2_payload(objects: list[dict[str, Any]]) -> bytes:
    prefix = b"".join(_canonical_line(item) for item in objects[:-1])
    footer = objects[-1]
    footer["record_count"] = sum(
        item["record_type"] not in {"header", "blob", "end"} for item in objects
    )
    footer["blob_count"] = sum(item["record_type"] == "blob" for item in objects)
    data = [
        item for item in objects if item["record_type"] not in {"header", "blob", "end"}
    ]
    footer["last_seq"] = data[-1]["seq"] if data else 0
    footer["stream_sha256"] = sha256_bytes(prefix)
    return prefix + _canonical_line(footer)


def _confirmed_payload(root: Path) -> bytes:
    store = _create_store(root)
    claim = store.propose_claim(
        proposition="A human-confirmed portable claim",
        claim_kind="fact",
        actor=HUMAN,
        created_at=NOW,
        status_at=NOW,
    ).claim
    lifecycle = TruthLifecycle(store)
    gesture = lifecycle.mint_gesture(
        subject_ref=claim.id,
        actor=HUMAN,
        surface="dashboard",
        kind="confirm",
        displayed_payload_sha256=claim.canonical_sha256,
        at=LATER,
    )
    lifecycle.confirm_claim(
        claim_id=claim.id,
        gesture_id=gesture.id,
        actor=HUMAN,
        expected_context_sha256=None,
        observed_at=LATER,
        at=LATER,
    )
    return export_store(store).path.read_bytes()


def _confirm_claim(
    store: TruthStore,
    claim: ClaimRecord,
    *,
    at: str,
) -> None:
    lifecycle = TruthLifecycle(store)
    gesture = lifecycle.mint_gesture(
        subject_ref=claim.id,
        actor=HUMAN,
        surface="dashboard",
        kind="confirm",
        displayed_payload_sha256=claim.canonical_sha256,
        at=at,
    )
    lifecycle.confirm_claim(
        claim_id=claim.id,
        gesture_id=gesture.id,
        actor=HUMAN,
        expected_context_sha256=None,
        observed_at=at,
    )


def _force_confirm_without_weakest_link(
    store: TruthStore,
    claim: ClaimRecord,
    *,
    at: str,
) -> None:
    lifecycle = TruthLifecycle(store)
    gesture = lifecycle.mint_gesture(
        subject_ref=claim.id,
        actor=HUMAN,
        surface="dashboard",
        kind="confirm",
        displayed_payload_sha256=claim.canonical_sha256,
        at=at,
    )
    with store.write_transaction() as conn:
        store._consume_gesture_locked(conn, gesture.id, consumed_at=at)
        store._insert_status_event_locked(
            conn,
            claim_id=claim.id,
            status="confirmed",
            actor=HUMAN,
            basis_kind="gesture",
            basis_ref=gesture.id,
            at=at,
        )


def _table_rows(store: TruthStore, table: str, order: str) -> list[dict[str, Any]]:
    conn = store.connect()
    try:
        return [
            dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")
        ]
    finally:
        conn.close()


def test_export_is_byte_deterministic_lossless_and_round_trips(tmp_path: Path) -> None:
    source = _populate_full_store(tmp_path / "source")
    first = export_store(source)
    first_bytes = first.path.read_bytes()
    second = export_store(source, tmp_path / "second.jsonl")

    assert first_bytes == second.path.read_bytes()
    assert first.sha256 == second.sha256 == sha256_bytes(first_bytes)
    objects = _objects(first_bytes)
    assert objects[0]["record_type"] == "header"
    assert objects[0]["format"] == FORMAT_NAME
    assert objects[0]["format_version"] == FORMAT_VERSION
    assert objects[0]["profile"]["extensions"]["privacy_scope"] == "private-test"
    data = [
        item for item in objects if item["record_type"] not in {"header", "blob", "end"}
    ]
    assert [item["seq"] for item in data] == sorted(item["seq"] for item in data)
    assert len([item for item in objects if item["record_type"] == "blob"]) == 1
    assert all(
        item["record_type"] not in {"projection", "claims_current"} for item in objects
    )
    redacted = next(
        item
        for item in data
        if item["record_type"] == "claim" and item["record"]["id"] == DERIVED_CLAIM_ID
    )
    assert redacted["record"]["proposition"] == "[redacted]"
    assert redacted["record"]["redacted_at"] == LATER

    target_root = tmp_path / "target"
    target_root.mkdir()
    registry = FakeRegistry()
    imported = import_store(first.path, target_root, registry=registry)
    restored = imported.store

    assert imported.source_format_version == FORMAT_VERSION
    assert restored.store_id == source.store_id
    assert registry.calls == [source.store_id]
    assert restored.profile.to_dict() == source.profile.to_dict()
    assert _table_rows(restored, "ledger_records", "seq") == _table_rows(
        source, "ledger_records", "seq"
    )
    durable_tables = {
        "evidence": "id",
        "evidence_spans": "id",
        "claims": "id",
        "derivations": "id",
        "derivation_premises": "derivation_id, premise_ref",
        "claim_links": "id",
        "link_retractions": "link_id",
        "claim_status_events": "seq",
        "gestures": "id",
        "redaction_events": "id",
        "sweeps": "id",
        "sweep_findings": "id",
    }
    for table, order in durable_tables.items():
        assert _table_rows(restored, table, order) == _table_rows(source, table, order)
    assert _table_rows(restored, "projections", "id") == []
    assert _table_rows(restored, "claims_current", "claim_id") == []

    digest = sha256_bytes(bytes(range(256)))
    assert (restored.paths.blobs / digest).read_bytes() == bytes(range(256))
    restored_export = export_store(restored, tmp_path / "restored.jsonl")
    assert restored_export.path.read_bytes() == first_bytes

    appended = restored.propose_claim(
        proposition="A post-import claim",
        claim_kind="fact",
        actor=HUMAN,
        record_id="12" * 16,
        status_event_id="13" * 16,
        created_at=LATER,
        status_at=LATER,
    ).claim
    conn = restored.connect()
    try:
        appended_ledger_seq = conn.execute(
            "SELECT seq FROM ledger_records WHERE record_type = 'claim' "
            "AND record_key = ?",
            (appended.id,),
        ).fetchone()[0]
        appended_status_seq = conn.execute(
            "SELECT seq FROM claim_status_events WHERE id = ?",
            ("13" * 16,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert appended_ledger_seq > first.last_seq
    assert appended_status_seq > max(
        row["seq"] for row in _table_rows(source, "claim_status_events", "seq")
    )


def test_empty_store_round_trip_and_existing_empty_sidecar(tmp_path: Path) -> None:
    source = _create_store(tmp_path / "source")
    exported = export_store(source)
    target = tmp_path / "target"
    target.mkdir()
    (target / ".wbuddy" / "cowork").mkdir(parents=True)

    result = import_store(exported.path, target, registry=FakeRegistry())

    assert result.record_count == 0
    assert result.blob_count == 0
    assert result.store.store_id == source.store_id
    assert result.store.paths.sidecar == (target / ".wbuddy" / "cowork").resolve()
    manifest = (target / ".wbuddy" / "manifest.yaml").read_text(encoding="utf-8")
    assert "cowork:" in manifest
    again = export_store(result.store, tmp_path / "again.jsonl")
    assert again.path.read_bytes() == exported.path.read_bytes()


def test_document_truth_policy_history_round_trips_and_rebuilds_projection(
    tmp_path: Path,
) -> None:
    source = _create_store(tmp_path / "policy-source")
    document = documents.register_document(
        source,
        path="docs/policy.md",
        title="Policy",
        document_class="co_authored",
        content_sha256=sha256_bytes(b"policy"),
        actor=HUMAN,
        at=NOW,
    )
    before = resolve_document_truth_policy(source, document.id)
    assert before.truth_mutable is True

    exported = export_store(source)
    objects = _objects(exported.path.read_bytes())
    record_types = {
        item["record_type"]
        for item in objects
        if item["record_type"] not in {"header", "blob", "end"}
    }
    assert {
        "interaction_contract_definition",
        "document_interaction_contract_assignment",
        "document_truth_activation_transition",
        "document_truth_admission_seal_event",
    } <= record_types

    target = tmp_path / "policy-target"
    target.mkdir()
    restored = import_store(
        exported.path,
        target,
        registry=FakeRegistry(),
    ).store
    after = resolve_document_truth_policy(restored, document.id)
    assert after.to_dict() == before.to_dict()
    with restored.connect() as conn:
        assert conn.execute(
            "SELECT state FROM document_truth_activation_current "
            "WHERE document_id = ?",
            (document.id,),
        ).fetchone()[0] == "enabled"
        assert conn.execute(
            "SELECT state FROM document_truth_admission_seals_current "
            "WHERE document_id = ?",
            (document.id,),
        ).fetchone()[0] == "committed"


def test_pending_document_admission_export_round_trips_fail_closed(
    tmp_path: Path,
) -> None:
    source = _create_store(tmp_path / "pending-policy-source")
    with source.write_transaction() as conn:
        document = documents.register_document(
            source,
            path="docs/pending-policy.md",
            title="Pending policy",
            document_class="co_authored",
            content_sha256=sha256_bytes(b"pending policy"),
            actor=HUMAN,
            at=NOW,
            conn=conn,
        )
        before = provision_document_policy(
            source,
            document_id=document.id,
            interaction_contract_id=WORKING_DOCUMENT_CONTRACT,
            initial_activation="disabled",
            actor=HUMAN,
            intent_id="pending-policy:create",
            coordinator_decision_id="pending-policy:provisional-decision",
            coordinator_decision_sha256="a" * 64,
            commit_admission=False,
            conn=conn,
        )
    assert before.admission_state == "pending"
    assert before.truth_mutable is False

    exported = export_store(source)
    target = tmp_path / "pending-policy-target"
    target.mkdir()
    restored = import_store(
        exported.path,
        target,
        registry=FakeRegistry(),
    ).store

    after = resolve_document_truth_policy(restored, document.id)
    assert after.to_dict() == before.to_dict()
    assert after.admission_state == "pending"
    assert after.truth_mutable is False
    reproduced = export_store(restored, tmp_path / "pending-policy-again.jsonl")
    assert reproduced.path.read_bytes() == exported.path.read_bytes()


@pytest.mark.parametrize(
    ("table", "error"),
    (
        (
            "document_truth_activation_current",
            "activation projection disagrees",
        ),
        (
            "document_truth_admission_seals_current",
            "admission projection disagrees",
        ),
    ),
)
def test_export_rejects_truth_policy_projection_history_disagreement(
    tmp_path: Path,
    table: str,
    error: str,
) -> None:
    source = _create_store(tmp_path / f"projection-drift-{table}")
    documents.register_document(
        source,
        path="docs/projection-integrity.md",
        title="Projection integrity",
        document_class="co_authored",
        content_sha256=sha256_bytes(b"projection integrity"),
        actor=HUMAN,
        at=NOW,
    )
    with source.connect() as conn:
        conn.execute(f"UPDATE {table} SET updated_at = ?", (LATER,))

    with pytest.raises(TruthExportError, match=error):
        export_store(source)


def test_import_recomputes_truth_activation_document_ledger_fence(
    tmp_path: Path,
) -> None:
    source = _create_store(tmp_path / "activation-fence-source")
    documents.register_document(
        source,
        path="docs/activation-fence.md",
        title="Activation fence",
        document_class="co_authored",
        content_sha256=sha256_bytes(b"activation fence"),
        actor=HUMAN,
        at=NOW,
    )
    objects = _objects(export_store(source).path.read_bytes())
    transition = next(
        item
        for item in objects
        if item["record_type"] == "document_truth_activation_transition"
    )
    transition["record"]["ledger_high_water_seq"] = 1
    transition["record"]["ledger_digest"] = "f" * 64
    target = tmp_path / "activation-fence-target"
    target.mkdir()

    with pytest.raises(TruthImportError, match="ledger fence does not match"):
        import_store(_v2_payload(objects), target, registry=FakeRegistry())


def test_import_upcasts_frozen_v1_inline_format(tmp_path: Path) -> None:
    source = _create_store(tmp_path / "source", store_id="20" * 16)
    source.capture_evidence(
        kind="document",
        source_locator="file:///inline.md",
        actor=HUMAN,
        acquisition_method="paste",
        content="inline v1 evidence",
        record_id=EVIDENCE_ID,
        acquired_at=NOW,
        created_at=NOW,
    )
    current = _objects(export_store(source).path.read_bytes())
    assert not any(item["record_type"] == "blob" for item in current)
    header = current[0]
    header["format_version"] = 1
    v1_records = []
    for item in current[1:-1]:
        if item["record_type"] in {"blob", "end"}:
            continue
        v1_records.append(
            {
                "record": item["record"],
                "record_type": item["record_type"],
                "seq": item["seq"],
            }
        )
    payload = b"".join(
        _canonical_line(item)
        for item in [
            header,
            *v1_records,
            {"record_count": len(v1_records), "record_type": "end"},
        ]
    )
    target = tmp_path / "target"
    target.mkdir()

    result = import_store(payload, target, registry=FakeRegistry())

    assert result.source_format_version == 1
    assert result.store.get_evidence(EVIDENCE_ID) is not None
    upgraded = _objects(result.store.paths.claims_export.read_bytes())
    assert upgraded[0]["format_version"] == FORMAT_VERSION
    assert upgraded[1]["record_key"] == EVIDENCE_ID


def test_registry_collision_and_nonempty_target_are_refused_before_writes(
    tmp_path: Path,
) -> None:
    source = _create_store(tmp_path / "source", store_id="30" * 16)
    exported = export_store(source)
    target = tmp_path / "target"
    target.mkdir()
    existing = tmp_path / "other-live-store"
    existing.mkdir()
    registry = FakeRegistry(paths={source.store_id: [existing]})

    with pytest.raises(StoreIdentityCollision, match="already registered"):
        import_store(exported.path, target, registry=registry)
    assert not (target / ".wbuddy" / "cowork").exists()

    sidecar = target / ".wbuddy" / "cowork"
    sidecar.mkdir(parents=True)
    (sidecar / "sentinel").write_text("keep", encoding="utf-8")
    with pytest.raises(TruthImportError, match="must be empty"):
        import_store(exported.path, target, registry=FakeRegistry())
    assert (sidecar / "sentinel").read_text(encoding="utf-8") == "keep"


def test_import_rejects_missing_or_redirected_target_root(tmp_path: Path) -> None:
    source = _create_store(tmp_path / "source")
    exported = export_store(source)

    with pytest.raises(TruthImportError, match="must already exist"):
        import_store(
            exported.path,
            tmp_path / "missing",
            registry=FakeRegistry(),
        )

    external = tmp_path / "external"
    external.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    try:
        (target / ".wbuddy").symlink_to(external, target_is_directory=True)
    except OSError:
        return
    with pytest.raises(TruthImportError, match="redirected or unsupported"):
        import_store(exported.path, target, registry=FakeRegistry())
    assert list(external.iterdir()) == []


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("record_key", "record_key"),
        ("inline_hash", "inline evidence"),
        ("foreign_ref", "missing evidence"),
        ("duplicate", "strictly ordered"),
    ],
)
def test_import_preflight_rejects_corrupt_records_without_touching_target(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    source = _populate_full_store(tmp_path / "source")
    objects = _objects(export_store(source).path.read_bytes())
    data = [
        item for item in objects if item["record_type"] not in {"header", "blob", "end"}
    ]
    if mutation == "record_key":
        data[0]["record_key"] = "ff" * 16
    elif mutation == "inline_hash":
        evidence = next(
            item
            for item in data
            if item["record_type"] == "evidence"
            and item["record"]["content"] is not None
        )
        evidence["record"]["content"] += " altered"
    elif mutation == "foreign_ref":
        span = next(item for item in data if item["record_type"] == "evidence_span")
        span["record"]["evidence_id"] = "ff" * 16
    else:
        duplicate = dict(data[0])
        insert_at = objects.index(data[0]) + 1
        objects.insert(insert_at, duplicate)
    payload = _v2_payload(objects)
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(TruthImportError, match=message):
        import_store(payload, target, registry=FakeRegistry())

    assert list(target.iterdir()) == []


def test_import_preserves_valid_human_gestured_confirmation(tmp_path: Path) -> None:
    payload = _confirmed_payload(tmp_path / "source")
    target = tmp_path / "target"
    target.mkdir()

    restored = import_store(payload, target, registry=FakeRegistry()).store

    statuses = _table_rows(restored, "claim_status_events", "seq")
    assert [row["status"] for row in statuses] == ["proposed", "confirmed"]
    assert statuses[-1]["actor_kind"] == "human"
    assert statuses[-1]["basis_kind"] == "gesture"


@pytest.mark.parametrize(
    ("mutation", "finding_code"),
    [
        ("rule_confirmation", "confirmation_without_human_gesture"),
        ("agent_confirmation", "confirmation_without_human_gesture"),
        ("actor_binding", "gesture_actor_mismatch"),
        ("subject_binding", "gesture_subject_mismatch"),
        ("payload_binding", "gesture_payload_mismatch"),
        ("confirmation_kind", "invalid_confirmation_gesture_kind"),
        ("unconsumed_gesture", "unconsumed_status_gesture"),
        ("proposal_basis", "invalid_proposal_basis"),
    ],
)
def test_import_rejects_tampered_status_authority_before_publication(
    tmp_path: Path,
    mutation: str,
    finding_code: str,
) -> None:
    objects = _objects(_confirmed_payload(tmp_path / "source"))
    status_records = [
        item for item in objects if item["record_type"] == "claim_status_event"
    ]
    proposed = next(
        item for item in status_records if item["record"]["status"] == "proposed"
    )
    confirmed = next(
        item for item in status_records if item["record"]["status"] == "confirmed"
    )
    gesture = next(item for item in objects if item["record_type"] == "gesture")

    if mutation == "rule_confirmation":
        confirmed["record"]["basis_kind"] = "rule"
        confirmed["record"]["basis_ref"] = confirmed["record"]["claim_id"]
    elif mutation == "agent_confirmation":
        confirmed["record"]["actor_kind"] = "agent_run"
    elif mutation == "actor_binding":
        confirmed["record"]["actor_ref"] = "another-human"
    elif mutation == "subject_binding":
        gesture["record"]["subject_ref"] = "ff" * 16
    elif mutation == "payload_binding":
        gesture["record"]["payload_sha256"] = "ff" * 32
    elif mutation == "confirmation_kind":
        gesture["record"]["kind"] = "redact"
    elif mutation == "unconsumed_gesture":
        gesture["record"]["consumed_at"] = None
    else:
        proposed["record"]["basis_ref"] = "ff" * 16

    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(TruthImportError, match=finding_code):
        import_store(_v2_payload(objects), target, registry=FakeRegistry())

    assert list(target.iterdir()) == []


def test_import_rejects_confirmed_derivation_with_unconfirmed_premise(
    tmp_path: Path,
) -> None:
    source = _create_store(tmp_path / "source")
    premise = source.propose_claim(
        proposition="Unconfirmed premise",
        claim_kind="fact",
        actor=HUMAN,
        created_at=NOW,
        status_at=NOW,
    ).claim
    conclusion = source.propose_claim(
        proposition="Invalid confirmed conclusion",
        claim_kind="fact",
        actor=HUMAN,
        created_at=NOW,
        status_at=NOW,
    ).claim
    source.add_derivation(
        claim_id=conclusion.id,
        method="deduction",
        premises=[premise.id],
        actor=HUMAN,
        created_at=NOW,
    )
    _force_confirm_without_weakest_link(source, conclusion, at=LATER)

    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(
        TruthImportError,
        match="confirmed_derivation_has_unconfirmed_premise",
    ):
        import_store(export_store(source).path, target, registry=FakeRegistry())

    assert list(target.iterdir()) == []


def test_import_round_trip_preserves_valid_confirmation_and_later_drift_warning(
    tmp_path: Path,
) -> None:
    source = _create_store(tmp_path / "source")
    premise = source.propose_claim(
        proposition="Premise valid at decision time",
        claim_kind="fact",
        actor=HUMAN,
        created_at=NOW,
        status_at=NOW,
    ).claim
    conclusion = source.propose_claim(
        proposition="Conclusion whose foundation later moves",
        claim_kind="fact",
        actor=HUMAN,
        created_at=NOW,
        status_at=NOW,
    ).claim
    successor = source.propose_claim(
        proposition="Replacement premise",
        claim_kind="fact",
        actor=HUMAN,
        valid_from=AFTER,
        created_at=NOW,
        status_at=NOW,
    ).claim
    _confirm_claim(source, premise, at=LATER)
    source.add_derivation(
        claim_id=conclusion.id,
        method="entailment",
        premises=[premise.id],
        actor=HUMAN,
        created_at=LATER,
    )
    # Premise and conclusion decisions share a timestamp. Ledger order proves
    # the premise was already authoritative for the conclusion confirmation.
    _confirm_claim(source, conclusion, at=LATER)
    lifecycle = TruthLifecycle(source)
    lifecycle.supersede_claim(
        successor_claim_id=successor.id,
        predecessor_claim_id=premise.id,
        reason="updated",
        actor=HUMAN,
        created_at=AFTER,
    )
    _confirm_claim(source, successor, at=AFTER)
    lifecycle.mark_needs_review(
        claim_id=conclusion.id,
        actor=SYSTEM,
        basis_kind="rule",
        basis_ref="premise-superseded",
        at=FINAL,
    )

    source_weakest_link = [
        item
        for item in integrity_findings(source)
        if item.code == "confirmed_derivation_has_unconfirmed_premise"
    ]
    assert len(source_weakest_link) == 1
    assert source_weakest_link[0].severity == "warning"

    target = tmp_path / "target"
    target.mkdir()
    restored = import_store(
        export_store(source).path,
        target,
        registry=FakeRegistry(),
    ).store

    restored_weakest_link = [
        item
        for item in integrity_findings(restored)
        if item.code == "confirmed_derivation_has_unconfirmed_premise"
    ]
    assert len(restored_weakest_link) == 1
    assert restored_weakest_link[0].severity == "warning"


def test_import_rejects_status_seq_that_reverses_canonical_ledger_order(
    tmp_path: Path,
) -> None:
    source = _create_store(tmp_path / "source")
    premise = source.propose_claim(
        proposition="Premise whose overlay must remain active",
        claim_kind="fact",
        actor=HUMAN,
        created_at=NOW,
        status_at=NOW,
    ).claim
    conclusion = source.propose_claim(
        proposition="Conclusion blocked by the active overlay",
        claim_kind="fact",
        actor=HUMAN,
        created_at=NOW,
        status_at=NOW,
    ).claim
    source.add_derivation(
        claim_id=conclusion.id,
        method="entailment",
        premises=[premise.id],
        actor=HUMAN,
        created_at=NOW,
    )
    _confirm_claim(source, premise, at=LATER)
    TruthLifecycle(source).mark_needs_review(
        claim_id=premise.id,
        actor=SYSTEM,
        basis_kind="rule",
        basis_ref="active-premise-review",
        at=LATER,
    )
    _force_confirm_without_weakest_link(source, conclusion, at=LATER)

    objects = _objects(export_store(source).path.read_bytes())
    premise_statuses = [
        item
        for item in objects
        if item["record_type"] == "claim_status_event"
        and item["record"]["claim_id"] == premise.id
    ]
    confirmed = next(
        item for item in premise_statuses if item["record"]["status"] == "confirmed"
    )
    overlay = next(
        item
        for item in premise_statuses
        if item["record"]["status"] == "needs_review"
    )
    confirmed["record"]["seq"], overlay["record"]["seq"] = (
        overlay["record"]["seq"],
        confirmed["record"]["seq"],
    )

    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(
        TruthImportError,
        match="status_sequence_ledger_order_mismatch",
    ):
        import_store(_v2_payload(objects), target, registry=FakeRegistry())

    assert list(target.iterdir()) == []


@pytest.mark.parametrize("followup", ["needs_review", "retracted"])
def test_import_rejects_historical_weakest_link_violation_after_later_states(
    tmp_path: Path,
    followup: str,
) -> None:
    source = _create_store(tmp_path / "source")
    premise = source.propose_claim(
        proposition=f"Premise confirmed too late for {followup}",
        claim_kind="fact",
        actor=HUMAN,
        created_at=NOW,
        status_at=NOW,
    ).claim
    conclusion = source.propose_claim(
        proposition=f"Invalid conclusion later {followup}",
        claim_kind="fact",
        actor=HUMAN,
        created_at=NOW,
        status_at=NOW,
    ).claim
    source.add_derivation(
        claim_id=conclusion.id,
        method="deduction",
        premises=[premise.id],
        actor=HUMAN,
        created_at=NOW,
    )
    _force_confirm_without_weakest_link(source, conclusion, at=LATER)
    _confirm_claim(source, premise, at=AFTER)

    lifecycle = TruthLifecycle(source)
    if followup == "needs_review":
        lifecycle.mark_needs_review(
            claim_id=conclusion.id,
            actor=SYSTEM,
            basis_kind="rule",
            basis_ref="late-premise-confirmation",
            at=FINAL,
        )
    else:
        gesture = lifecycle.mint_gesture(
            subject_ref=conclusion.id,
            actor=HUMAN,
            surface="dashboard",
            kind="redact",
            displayed_payload_sha256=conclusion.canonical_sha256,
            at=FINAL,
        )
        TruthRedactor(source, lifecycle=lifecycle).redact(
            subject_kind="claim",
            subject_ref=conclusion.id,
            actor=HUMAN,
            reason="privacy",
            basis_kind="gesture",
            basis_ref=gesture.id,
            at=FINAL,
        )

    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(
        TruthImportError,
        match="confirmed_derivation_has_unconfirmed_premise",
    ):
        import_store(export_store(source).path, target, registry=FakeRegistry())

    assert list(target.iterdir()) == []


def test_import_preserves_warning_only_external_premise(tmp_path: Path) -> None:
    source = _create_store(tmp_path / "source")
    conclusion = source.propose_claim(
        proposition="Portable unresolved external premise",
        claim_kind="fact",
        actor=HUMAN,
        created_at=NOW,
        status_at=NOW,
    ).claim
    source.add_derivation(
        claim_id=conclusion.id,
        method="federated",
        premises=[truth_uri(new_id(), "claim", new_id())],
        actor=HUMAN,
        created_at=NOW,
    )
    target = tmp_path / "target"
    target.mkdir()

    restored = import_store(
        export_store(source).path,
        target,
        registry=FakeRegistry(),
    ).store

    assert restored.get_claim(conclusion.id) is not None


def test_import_rejects_newer_malformed_duplicate_header_and_trailing_records(
    tmp_path: Path,
) -> None:
    source = _create_store(tmp_path / "source")
    payload = export_store(source).path.read_bytes()
    objects = _objects(payload)
    objects[0]["format_version"] = FORMAT_VERSION + 1
    newer = b"".join(_canonical_line(item) for item in objects)
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(TruthImportError, match="newer"):
        import_store(newer, target, registry=FakeRegistry())
    with pytest.raises(TruthImportError, match="malformed JSON"):
        import_store(b"{not-json}\n", target, registry=FakeRegistry())
    with pytest.raises(TruthImportError, match="trailing"):
        import_store(
            payload + _canonical_line({"record_type": "end"}),
            target,
            registry=FakeRegistry(),
        )
    duplicate_header = payload.replace(
        f'"format_version":{FORMAT_VERSION}'.encode("ascii"),
        (
            f'"format_version":{FORMAT_VERSION},'
            f'"format_version":{FORMAT_VERSION}'
        ).encode("ascii"),
        1,
    )
    with pytest.raises(TruthImportError, match="malformed JSON"):
        import_store(duplicate_header, target, registry=FakeRegistry())
    assert list(target.iterdir()) == []


def test_older_schema_export_rebuilds_under_a_newer_engine(tmp_path: Path) -> None:
    source = _create_store(tmp_path / "current-source")
    claim = source.propose_claim(
        proposition="Portable history outlives the SQLite schema",
        claim_kind="fact",
        actor=HUMAN,
        created_at=NOW,
        status_at=NOW,
    ).claim
    # Simulate an older-schema export: the same portable records with a
    # store_info schema_version of 1. The JSONL format, not the SQLite schema
    # version, governs the portable record contract, so the current engine
    # upcasts the stream and rebuilds it under the current v2 schema DDL.
    objects = _objects(export_store(source).path.read_bytes())
    objects[0]["store_info"]["schema_version"] = 1
    older_payload = _v2_payload(objects)
    assert _objects(older_payload)[0]["store_info"]["schema_version"] == 1

    target = tmp_path / "v2-target"
    target.mkdir()

    restored = import_store(older_payload, target, registry=FakeRegistry()).store

    assert restored.get_claim(claim.id).canonical_sha256 == claim.canonical_sha256
    with restored.connect() as conn:
        assert (
            conn.execute("PRAGMA user_version").fetchone()[0]
            == truth_migrations.SCHEMA_VERSION
        )
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'documents'"
        ).fetchone()
    header = _objects(restored.paths.claims_export.read_bytes())[0]
    assert (
        header["store_info"]["schema_version"]
        == truth_migrations.SCHEMA_VERSION
    )


def test_staging_failure_is_not_published_and_existing_empty_target_is_restored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _create_store(tmp_path / "source")
    exported = export_store(source)
    target = tmp_path / "target"
    target.mkdir()
    sidecar = target / ".wbuddy" / "cowork"
    sidecar.mkdir(parents=True)

    def fail_insert(*args, **kwargs):
        raise RuntimeError("staged insert failed")

    monkeypatch.setattr("work_buddy.truth.export._insert_records", fail_insert)
    with pytest.raises(RuntimeError, match="staged insert failed"):
        import_store(exported.path, target, registry=FakeRegistry())

    assert sidecar.is_dir()
    assert list(sidecar.iterdir()) == []
    assert not list(target.glob(".wbuddy-cowork-import-*"))


def test_export_refuses_missing_blob_and_unordered_base_rows(tmp_path: Path) -> None:
    store = _create_store(tmp_path / "source", inline_content_bytes=0)
    evidence = store.capture_evidence(
        kind="artifact",
        source_locator="file:///blob.bin",
        actor=HUMAN,
        acquisition_method="paste",
        content=b"blob bytes",
        record_id=BLOB_EVIDENCE_ID,
        acquired_at=NOW,
        created_at=NOW,
    )
    assert evidence.content_path is not None
    store.resolve_blob_path(evidence.content_path).unlink()
    with pytest.raises(TruthExportError, match="unavailable"):
        export_store(store)

    other = _create_store(tmp_path / "other")
    conn = other.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO sweeps (id, kind, at, params_json) VALUES (?, 'integrity', ?, '{}')",
            (SWEEP_ID, NOW),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    with pytest.raises(TruthExportError, match="missing from ledger_records"):
        export_store(other)


def test_export_publication_cannot_regress_behind_a_newer_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from work_buddy.truth import export as export_module

    store = _create_store(tmp_path / "publication-lock")
    store.propose_claim(
        proposition="First committed claim",
        claim_kind="fact",
        actor=HUMAN,
        created_at=NOW,
        status_at=NOW,
    )

    first_publish_entered = threading.Event()
    release_first_publish = threading.Event()
    second_writer_started = threading.Event()
    second_writer_done = threading.Event()
    original_atomic_write = export_module.atomic_write_bytes
    publish_count = 0
    publish_count_lock = threading.Lock()

    def paused_atomic_write(path: Path, payload: bytes) -> None:
        nonlocal publish_count
        with publish_count_lock:
            publish_count += 1
            ordinal = publish_count
        if ordinal == 1:
            first_publish_entered.set()
            assert release_first_publish.wait(timeout=10)
        original_atomic_write(path, payload)

    monkeypatch.setattr(export_module, "atomic_write_bytes", paused_atomic_write)

    def publish_old_snapshot() -> None:
        export_store(store)

    def commit_and_publish_newer_snapshot() -> None:
        second_writer_started.set()
        store.propose_claim(
            proposition="Second committed claim",
            claim_kind="fact",
            actor=HUMAN,
            created_at=LATER,
            status_at=LATER,
        )
        second_writer_done.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        older = executor.submit(publish_old_snapshot)
        assert first_publish_entered.wait(timeout=10)
        newer = executor.submit(commit_and_publish_newer_snapshot)
        assert second_writer_started.wait(timeout=10)
        assert not second_writer_done.wait(timeout=0.2)
        release_first_publish.set()
        older.result(timeout=10)
        newer.result(timeout=10)

    footer = _objects(store.paths.claims_export.read_bytes())[-1]
    with store.connect() as conn:
        db_last_seq = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM ledger_records"
        ).fetchone()[0]
    assert footer["last_seq"] == db_last_seq


def test_failed_automatic_export_surfaces_after_commit_and_removes_stale_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import work_buddy.truth.export as export_module

    store = _create_store(tmp_path / "failed-hook")
    assert store.paths.claims_export.is_file()
    claim_id = "11" * 16

    def fail_publication(path: Path, payload: bytes) -> None:
        raise OSError("forced publication failure")

    monkeypatch.setattr(export_module, "atomic_write_bytes", fail_publication)
    with pytest.raises(PostCommitHookError, match="commit succeeded"):
        store.propose_claim(
            proposition="The row commits before its recovery export fails",
            claim_kind="fact",
            actor=HUMAN,
            record_id=claim_id,
            created_at=NOW,
            status_at=NOW,
        )

    assert store.get_claim(claim_id) is not None
    assert not store.paths.claims_export.exists()


def _portable_specialist_job_row(
    *,
    role: str = "specialist",
    assignment: dict[str, Any] | None = None,
    include_assignment_field: bool = True,
) -> dict[str, Any]:
    selection = {
        "provider_id": "codex",
        "model_id": "test-model",
        "provider_label": "Codex",
        "model_label": "Test model",
    }
    request = {
        "schema": "work-buddy.cowork-coordination-request/v1",
        "user_goal": "Evaluate one admitted check.",
        "protected_intent": "Preserve the captured target.",
        "effective_configuration": None,
        "effective_configuration_sha256": None,
        "effective_policy_sha256": None,
        "active_criterion_ids": [],
        "prior_disposition_ids": [],
        "prior_human_review_outcome_ids": [],
        "recheck_of_run_id": None,
        "recheck_of_proposal_ids": [],
        "recheck_intent_id": None,
        "coordinator_stage": None,
        "requested_revision_result_ids": [],
        "candidate_evaluations": [],
    }
    if include_assignment_field:
        request["specialist_assignment"] = assignment
    payload = {
        "document_id": "81" * 16,
        "evaluation_run_id": "82" * 16,
        "action_snapshot_id": "83" * 16,
        "plan_snapshot_id": "84" * 16,
        "role": role,
        "parent_job_id": None,
        "authorization_receipt_id": "85" * 16,
        "context_sha256": "86" * 32,
        "selection": selection,
        "request_summary": request,
    }
    return {
        "id": "87" * 16,
        **{
            key: payload[key]
            for key in (
                "document_id",
                "evaluation_run_id",
                "action_snapshot_id",
                "plan_snapshot_id",
                "role",
                "parent_job_id",
                "authorization_receipt_id",
                "context_sha256",
            )
        },
        "selection_json": canonical_json(selection),
        "request_summary_json": canonical_json(request),
        "canonical_sha256": sha256_bytes(
            canonical_json(payload).encode("utf-8")
        ),
        "created_at": NOW,
        "created_by_kind": "system",
        "created_by_ref": "portable-specialist-test",
        "created_by_meta_json": None,
    }


def _portable_specialist_status_row(
    refs: dict[str, Any],
) -> dict[str, Any]:
    payload = {
        "coordination_job_id": "87" * 16,
        "status": "completed",
        "outcome_kind": "typed_submission_received",
        "output_sha256": "88" * 32,
        "error_code": None,
        "message": None,
        "consequence_refs": refs,
    }
    return {
        "id": "89" * 16,
        "coordination_job_id": payload["coordination_job_id"],
        "status": payload["status"],
        "outcome_kind": payload["outcome_kind"],
        "output_sha256": payload["output_sha256"],
        "error_code": None,
        "message": None,
        "consequence_refs_json": canonical_json(refs),
        "canonical_sha256": sha256_bytes(
            canonical_json(payload).encode("utf-8")
        ),
        "created_at": NOW,
        "created_by_kind": "system",
        "created_by_ref": "portable-specialist-test",
        "created_by_meta_json": None,
    }


def test_import_validation_accepts_portable_specialist_assignment_and_lineage():
    assignment = {
        "criterion_definition_version_id": "91" * 16,
        "check_definition_version_id": "92" * 16,
        "criterion_check_binding_id": "93" * 16,
        "sequence": 1,
        "total": 1,
        "configuration_sha256": "94" * 32,
    }
    job = _portable_specialist_job_row(assignment=assignment)
    status = _portable_specialist_status_row(
        {
            "check_execution_ids": ["95" * 16],
            "evaluation_result_ids": ["96" * 16],
        }
    )

    truth_export._validate_record_values(
        truth_export._DataRecord(
            seq=1,
            record_type="cowork_coordination_job",
            record_key=job["id"],
            record=job,
        )
    )
    truth_export._validate_record_values(
        truth_export._DataRecord(
            seq=2,
            record_type="cowork_coordination_status_event",
            record_key=status["id"],
            record=status,
        )
    )


def test_import_validation_keeps_pre_assignment_coordination_bytes_valid():
    row = _portable_specialist_job_row(include_assignment_field=False)

    truth_export._validate_record_values(
        truth_export._DataRecord(
            seq=1,
            record_type="cowork_coordination_job",
            record_key=row["id"],
            record=row,
        )
    )
    assert "specialist_assignment" not in json.loads(
        row["request_summary_json"]
    )


@pytest.mark.parametrize(
    ("assignment_update", "match"),
    [
        ({"sequence": 0}, "sequence must be at least 1"),
        ({"sequence": 2}, "sequence cannot exceed total"),
        (
            {"configuration_sha256": "not-a-digest"},
            "configuration_sha256 must be a lowercase SHA-256 digest",
        ),
    ],
)
def test_import_validation_rejects_invalid_portable_specialist_assignment(
    assignment_update: dict[str, Any],
    match: str,
):
    assignment = {
        "criterion_definition_version_id": "91" * 16,
        "check_definition_version_id": "92" * 16,
        "criterion_check_binding_id": "93" * 16,
        "sequence": 1,
        "total": 1,
        "configuration_sha256": "94" * 32,
        **assignment_update,
    }
    row = _portable_specialist_job_row(assignment=assignment)

    with pytest.raises(TruthImportError, match=match):
        truth_export._validate_record_values(
            truth_export._DataRecord(
                seq=1,
                record_type="cowork_coordination_job",
                record_key=row["id"],
                record=row,
            )
        )


def test_import_validation_rejects_malformed_specialist_lineage_refs():
    row = _portable_specialist_status_row(
        {
            "check_execution_ids": ["not-an-id"],
            "evaluation_result_ids": [],
        }
    )

    with pytest.raises(
        TruthImportError,
        match="check_execution_ids must be a lowercase 32-hex id",
    ):
        truth_export._validate_record_values(
            truth_export._DataRecord(
                seq=1,
                record_type="cowork_coordination_status_event",
                record_key=row["id"],
                record=row,
            )
        )


def _portable_specialist_lineage_records(
    *,
    boundary_update: dict[str, Any] | None = None,
    receipt_update: dict[str, Any] | None = None,
    assignment_update: dict[str, Any] | None = None,
    plan_check_update: dict[str, Any] | None = None,
    binding_configuration: dict[str, Any] | None = None,
    execution_ref: str = "assigned",
    result_ref: str = "assigned",
    producer_job_id: str | None = None,
    assigned_execution_mode: str = "specialist",
    include_next_ref: bool = True,
    next_role: str = "coordinator",
    next_parent_id: str | None = None,
    next_stage: str = "initial",
    extra_assigned_result: bool = False,
) -> tuple[truth_export._DataRecord, ...]:
    document_id = "81" * 16
    run_id = "82" * 16
    action_id = "83" * 16
    plan_id = "84" * 16
    receipt_id = "85" * 16
    context_sha256 = "86" * 32
    job_id = "87" * 16
    coordinator_job_id = "88" * 16
    coordinator_receipt_id = "8a" * 16
    coordinator_context_sha256 = "8b" * 32
    deterministic_defaults = terminology_exact_match_defaults(at=NOW)
    criterion_id = (
        "91" * 16
        if assigned_execution_mode == "specialist"
        else deterministic_defaults.criterion.id
    )
    criterion_kind = (
        "user_authored"
        if assigned_execution_mode == "specialist"
        else deterministic_defaults.criterion.criterion_kind
    )
    admitted_check = (
        instruction_model_check_defaults(at=NOW)
        if assigned_execution_mode == "specialist"
        else deterministic_defaults.check
    )
    check_id = admitted_check.id
    binding_id = "93" * 16
    configuration = {"instructions": "Review the captured target."}
    configuration_sha256 = sha256_bytes(
        canonical_json(configuration).encode("utf-8")
    )
    assigned_execution_id = "95" * 16
    assigned_result_id = "96" * 16
    alternate_criterion_id = "a1" * 16
    alternate_check_id = "a2" * 16
    alternate_binding_id = "a3" * 16
    alternate_execution_id = "a4" * 16
    alternate_result_id = "a5" * 16
    target_text_sha256 = "b1" * 32
    assignment = {
        "criterion_definition_version_id": criterion_id,
        "check_definition_version_id": check_id,
        "criterion_check_binding_id": binding_id,
        "sequence": 1,
        "total": 1,
        "configuration_sha256": configuration_sha256,
        **(assignment_update or {}),
    }
    job = _portable_specialist_job_row(assignment=assignment)
    request_summary = json.loads(job["request_summary_json"])
    selection = json.loads(job["selection_json"])
    authority_context = {
        field: request_summary[field]
        for field in (
            "user_goal",
            "protected_intent",
            "effective_configuration",
            "effective_configuration_sha256",
            "effective_policy_sha256",
            "active_criterion_ids",
            "prior_disposition_ids",
            "prior_human_review_outcome_ids",
            "recheck_of_run_id",
            "recheck_of_proposal_ids",
            "recheck_intent_id",
            "coordinator_stage",
            "requested_revision_result_ids",
            "specialist_assignment",
        )
    }
    boundary = {
        "role": "specialist",
        "job_id": job_id,
        "document": "captured_target_only",
        "action_snapshot_id": action_id,
        "authority_context": authority_context,
        **(boundary_update or {}),
    }
    plan_check = {
        "criterion_definition_version_id": criterion_id,
        "check_definition_version_id": check_id,
        "criterion_check_binding_id": binding_id,
        "criterion_activation_id": "a6" * 16,
        "configuration_sha256": configuration_sha256,
        **(plan_check_update or {}),
    }
    assigned_execution = {
        "id": assigned_execution_id,
        "evaluation_run_id": run_id,
        "check_definition_version_id": check_id,
        "criterion_check_binding_id": binding_id,
        "input_sha256": target_text_sha256,
        "producer_json": canonical_json(
            {
                "kind": "account_backed_specialist",
                "job_id": producer_job_id or job_id,
            }
        ),
    }
    alternate_execution = {
        "id": alternate_execution_id,
        "evaluation_run_id": run_id,
        "check_definition_version_id": alternate_check_id,
        "criterion_check_binding_id": alternate_binding_id,
        "input_sha256": target_text_sha256,
        "producer_json": canonical_json(
            {
                "kind": "account_backed_specialist",
                "job_id": job_id,
            }
        ),
    }
    execution_id = (
        assigned_execution_id
        if execution_ref == "assigned"
        else alternate_execution_id
    )
    result_id = (
        assigned_result_id if result_ref == "assigned" else alternate_result_id
    )
    completed_refs = {
        "check_execution_ids": [execution_id],
        "evaluation_result_ids": [result_id],
    }
    if include_next_ref:
        completed_refs["next_job_id"] = coordinator_job_id
    completed = _portable_specialist_status_row(completed_refs)
    coordinator_request = deepcopy(request_summary)
    coordinator_request["specialist_assignment"] = None
    coordinator_request["coordinator_stage"] = next_stage
    coordinator_authority_context = {
        field: coordinator_request[field]
        for field in authority_context
    }
    coordinator_payload = {
        "document_id": document_id,
        "evaluation_run_id": run_id,
        "action_snapshot_id": action_id,
        "plan_snapshot_id": plan_id,
        "role": next_role,
        "parent_job_id": next_parent_id,
        "authorization_receipt_id": coordinator_receipt_id,
        "context_sha256": coordinator_context_sha256,
        "selection": selection,
        "request_summary": coordinator_request,
    }
    coordinator_job = {
        "id": coordinator_job_id,
        "document_id": document_id,
        "evaluation_run_id": run_id,
        "action_snapshot_id": action_id,
        "plan_snapshot_id": plan_id,
        "role": next_role,
        "parent_job_id": next_parent_id,
        "authorization_receipt_id": coordinator_receipt_id,
        "context_sha256": coordinator_context_sha256,
        "selection_json": canonical_json(selection),
        "request_summary_json": canonical_json(coordinator_request),
        "canonical_sha256": sha256_bytes(
            canonical_json(coordinator_payload).encode("utf-8")
        ),
    }

    rows = [
        ("document", document_id, {"id": document_id}),
        (
            "criterion_definition_version",
            criterion_id,
            {"id": criterion_id, "criterion_kind": criterion_kind},
        ),
        (
            "criterion_definition_version",
            alternate_criterion_id,
            {"id": alternate_criterion_id},
        ),
        (
            "check_definition_version",
            check_id,
            {
                "id": check_id,
                "canonical_sha256": admitted_check.canonical_sha256,
                "executor_ref": admitted_check.executor_ref,
                "mechanism": admitted_check.mechanism,
                "supported_criterion_kinds_json": (
                    admitted_check.supported_criterion_kinds_json
                ),
            },
        ),
        (
            "check_definition_version",
            alternate_check_id,
            {"id": alternate_check_id},
        ),
        (
            "criterion_check_binding",
            binding_id,
            {
                "id": binding_id,
                "criterion_definition_version_id": criterion_id,
                "check_definition_version_id": check_id,
                "configuration_json": canonical_json(
                    binding_configuration
                    if binding_configuration is not None
                    else configuration
                ),
            },
        ),
        (
            "criterion_check_binding",
            alternate_binding_id,
            {
                "id": alternate_binding_id,
                "criterion_definition_version_id": alternate_criterion_id,
                "check_definition_version_id": alternate_check_id,
                "configuration_json": canonical_json(
                    {"instructions": "Alternate check."}
                ),
            },
        ),
        (
            "action_snapshot",
            action_id,
            {
                "id": action_id,
                "document_id": document_id,
                "document_version_id": None,
                "target_text_sha256": target_text_sha256,
            },
        ),
        (
            "evaluation_plan_snapshot",
            plan_id,
            {
                "id": plan_id,
                "action_snapshot_id": action_id,
                "plan_json": canonical_json(
                    {
                        "schema": "work-buddy.cowork-evaluation-plan/v1",
                        "action_snapshot_id": action_id,
                        "checks": [plan_check],
                    }
                ),
            },
        ),
        (
            "evaluation_run",
            run_id,
            {
                "id": run_id,
                "action_snapshot_id": action_id,
                "plan_snapshot_id": plan_id,
            },
        ),
        ("check_execution", assigned_execution_id, assigned_execution),
        ("check_execution", alternate_execution_id, alternate_execution),
        (
            "evaluation_result",
            assigned_result_id,
            {
                "id": assigned_result_id,
                "evaluation_run_id": run_id,
                "check_execution_id": assigned_execution_id,
                "criterion_definition_version_id": criterion_id,
            },
        ),
        (
            "evaluation_result",
            alternate_result_id,
            {
                "id": alternate_result_id,
                "evaluation_run_id": run_id,
                "check_execution_id": alternate_execution_id,
                "criterion_definition_version_id": alternate_criterion_id,
            },
        ),
        *(
            [
                (
                    "evaluation_result",
                    "a9" * 16,
                    {
                        "id": "a9" * 16,
                        "evaluation_run_id": run_id,
                        "check_execution_id": assigned_execution_id,
                        "criterion_definition_version_id": criterion_id,
                    },
                )
            ]
            if extra_assigned_result
            else []
        ),
        (
            "model_call_authorization_receipt",
            receipt_id,
            {
                "id": receipt_id,
                "action_snapshot_id": action_id,
                "plan_snapshot_id": plan_id,
                "provider": selection["provider_id"],
                "model": selection["model_id"],
                "context_sha256": context_sha256,
                "content_boundary_json": canonical_json(boundary),
                "egress_class": "account_backed_agent",
                "cost_ceiling_usd": 2.0,
                "retry_limit": 0,
                "created_by_kind": "human",
                **(receipt_update or {}),
            },
        ),
        ("cowork_coordination_job", job_id, job),
        (
            "model_call_authorization_receipt",
            coordinator_receipt_id,
            {
                "id": coordinator_receipt_id,
                "action_snapshot_id": action_id,
                "plan_snapshot_id": plan_id,
                "provider": selection["provider_id"],
                "model": selection["model_id"],
                "context_sha256": coordinator_context_sha256,
                "content_boundary_json": canonical_json(
                    {
                        "role": next_role,
                        "job_id": coordinator_job_id,
                        "document": (
                            "complete_permitted_frozen_projection"
                        ),
                        "action_snapshot_id": action_id,
                        "authority_context": coordinator_authority_context,
                    }
                ),
                "egress_class": "account_backed_agent",
                "cost_ceiling_usd": 2.0,
                "retry_limit": 0,
                "created_by_kind": "human",
            },
        ),
        (
            "cowork_coordination_job",
            coordinator_job_id,
            coordinator_job,
        ),
        (
            "cowork_coordination_status_event",
            "a7" * 16,
            {
                "id": "a7" * 16,
                "coordination_job_id": job_id,
                "status": "prepared",
                "outcome_kind": None,
                "consequence_refs_json": canonical_json({}),
            },
        ),
        (
            "cowork_coordination_status_event",
            "a8" * 16,
            {
                "id": "a8" * 16,
                "coordination_job_id": job_id,
                "status": "submitted",
                "outcome_kind": "typed_submission_received",
                "consequence_refs_json": canonical_json({}),
            },
        ),
        (
            "cowork_coordination_status_event",
            completed["id"],
            completed,
        ),
    ]
    return tuple(
        truth_export._DataRecord(
            seq=seq,
            record_type=record_type,
            record_key=record_key,
            record=row,
        )
        for seq, (record_type, record_key, row) in enumerate(rows, start=1)
    )


def test_import_foreign_refs_accept_exact_specialist_authorization_and_lineage():
    truth_export._validate_foreign_refs(
        _portable_specialist_lineage_records()
    )


@pytest.mark.parametrize(
    "boundary_update",
    [
        {"role": "coordinator"},
        {"job_id": "c1" * 16},
        {"document": "complete_permitted_frozen_projection"},
        {"action_snapshot_id": "c2" * 16},
        {"authority_context": {"specialist_assignment": None}},
    ],
)
def test_import_foreign_refs_reject_mismatched_specialist_authorization(
    boundary_update: dict[str, Any],
):
    with pytest.raises(
        TruthImportError,
        match="exact authorization boundary",
    ):
        truth_export._validate_foreign_refs(
            _portable_specialist_lineage_records(
                boundary_update=boundary_update
            )
        )


@pytest.mark.parametrize(
    "receipt_update",
    [
        {"cost_ceiling_usd": 1.0},
        {"retry_limit": 1},
        {"created_by_kind": "system"},
    ],
)
def test_import_foreign_refs_reject_specialist_authorization_policy_tampering(
    receipt_update: dict[str, Any],
):
    with pytest.raises(
        TruthImportError,
        match="exact authorization boundary",
    ):
        truth_export._validate_foreign_refs(
            _portable_specialist_lineage_records(
                receipt_update=receipt_update
            )
        )


@pytest.mark.parametrize(
    "assignment_update",
    [
        {"sequence": 2, "total": 2},
        {"total": 2},
    ],
)
def test_import_foreign_refs_reject_specialist_sequence_tampering(
    assignment_update: dict[str, Any],
):
    with pytest.raises(
        TruthImportError,
        match="exact admitted specialist sequence",
    ):
        truth_export._validate_foreign_refs(
            _portable_specialist_lineage_records(
                assignment_update=assignment_update
            )
        )


def test_import_foreign_refs_rejects_deterministic_check_relabelled_specialist():
    with pytest.raises(
        TruthImportError,
        match="exact admitted specialist sequence",
    ):
        truth_export._validate_foreign_refs(
            _portable_specialist_lineage_records(
                assigned_execution_mode="deterministic"
            )
        )


@pytest.mark.parametrize(
    ("updates", "match"),
    [
        (
            {"include_next_ref": False},
            "next job",
        ),
        (
            {"next_role": "reviser"},
            "initial coordinator",
        ),
        (
            {"next_parent_id": "87" * 16},
            "initial coordinator",
        ),
        (
            {"next_stage": "post_revision"},
            "initial coordinator",
        ),
        (
            {"extra_assigned_result": True},
            "complete result set",
        ),
    ],
)
def test_import_foreign_refs_rejects_specialist_handoff_tampering(
    updates: dict[str, Any],
    match: str,
):
    with pytest.raises(TruthImportError, match=match):
        truth_export._validate_foreign_refs(
            _portable_specialist_lineage_records(**updates)
        )


@pytest.mark.parametrize(
    ("updates", "match"),
    [
        (
            {"plan_check_update": {"configuration_sha256": "c3" * 32}},
            "immutable binding",
        ),
        (
            {
                "binding_configuration": {
                    "instructions": "A different immutable configuration."
                }
            },
            "immutable binding",
        ),
        (
            {"execution_ref": "alternate"},
            "exact job assignment",
        ),
        (
            {"producer_job_id": "c4" * 16},
            "exact job assignment",
        ),
        (
            {"result_ref": "alternate"},
            "assigned execution lineage",
        ),
    ],
)
def test_import_foreign_refs_reject_crossed_specialist_lineage(
    updates: dict[str, Any],
    match: str,
):
    with pytest.raises(TruthImportError, match=match):
        truth_export._validate_foreign_refs(
            _portable_specialist_lineage_records(**updates)
        )
