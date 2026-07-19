"""Export format v3 tests: the co-work document surface and ydoc snapshots."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from work_buddy.truth.contracts import Actor
from work_buddy.truth.export import (
    FORMAT_VERSION,
    TruthExportError,
    TruthImportError,
    export_store,
    import_store,
)
from work_buddy.truth.identity import new_id, sha256_bytes, sha256_text
from work_buddy.truth.migrations import REDACTED_SELECTOR_JSON
from work_buddy.truth.proposals import proposal_canonical_sha256
from work_buddy.truth.redact import policy_basis_ref
from work_buddy.truth.store import TruthStore


NOW = "2026-07-17T16:00:00.000+00:00"
HUMAN = Actor("human", "user-1")

DOC_ID = "a1" * 16
SPAN_ID = "a2" * 16
EXPR_ID = "a3" * 16
PROP_ID = "a4" * 16
PSE_ID = "a5" * 16
DE_REG_ID = "a6" * 16
DE_MAT_ID = "a7" * 16
CLAIM_REF = "a8" * 16

SNAPSHOT_BYTES = b"opaque-ydoc-snapshot-v3-bytes"
SNAPSHOT_SHA = sha256_bytes(SNAPSHOT_BYTES)
CONTENT_SHA = sha256_text("materialized markdown body")
CLAIM_CANON = sha256_text("claim canonical")
SPAN_SHA = sha256_text("span fingerprint")
PROP_SPAN_SHA = sha256_text("proposal span")
# The stored canonical is the engine hash of the seeded proposal fields, so the
# import round-trip passes the live proposal-canonical-mismatch integrity check.
PROP_CANON = proposal_canonical_sha256(
    document_id=DOC_ID,
    base_content_sha256=CONTENT_SHA,
    selector={"exact": "old text"},
    quote_exact="old text",
    replacement="new text",
    rationale="reason",
    tldr="tldr",
    claim_refs=[{"claim": CLAIM_REF, "role": "instantiation"}],
)
PROP_DEDUP = sha256_text("proposal dedup")


def _profile(store_id: str | None = None) -> dict[str, Any]:
    return {
        "store_id": store_id or new_id(),
        "profile": "cothink-doc",
        "title": "Co-work doc store",
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


def _create_store(root: Path, *, store_id: str | None = None) -> TruthStore:
    root.mkdir(exist_ok=True)
    return TruthStore.create(root, _profile(store_id), inline_content_bytes=64)


def _write_blob(store: TruthStore, digest: str, data: bytes) -> None:
    (store.paths.blobs / digest).write_bytes(data)


def _insert_document(
    store: TruthStore,
    *,
    ydoc_snapshot_sha256: str | None = SNAPSHOT_SHA,
) -> None:
    """Seed one document plus its span, expression, proposal, and events."""
    with store.write_transaction() as conn:
        conn.execute(
            "INSERT INTO documents "
            "(id, path, title, document_class, content_sha256, "
            "ydoc_snapshot_sha256, created_at, created_by_kind, created_by_ref, "
            "meta_json) VALUES (?, 'docs/design.md', 'Design', 'co_authored', "
            "?, ?, ?, 'human', 'user-1', ?)",
            (
                DOC_ID,
                CONTENT_SHA,
                ydoc_snapshot_sha256,
                NOW,
                json.dumps({"producer": {"harness": "test"}}),
            ),
        )
        store._insert_ledger_record_locked(conn, "document", DOC_ID)
        conn.execute(
            "INSERT INTO document_spans "
            "(id, document_id, selector_json, quote_exact, span_sha256, "
            "author_kind, author_ref, created_at, created_by_kind, "
            "created_by_ref) VALUES (?, ?, ?, 'passage', ?, 'human', 'user-1', "
            "?, 'human', 'user-1')",
            (SPAN_ID, DOC_ID, json.dumps({"exact": "passage"}), SPAN_SHA, NOW),
        )
        store._insert_ledger_record_locked(conn, "document_span", SPAN_ID)
        conn.execute(
            "INSERT INTO expressions "
            "(id, document_span_id, claim_ref_kind, claim_ref, role, "
            "claim_canonical_sha256, span_sha256, created_at, created_by_kind, "
            "created_by_ref, meta_json) VALUES (?, ?, 'local', ?, "
            "'instantiation', ?, ?, ?, 'human', 'user-1', NULL)",
            (EXPR_ID, SPAN_ID, CLAIM_REF, CLAIM_CANON, SPAN_SHA, NOW),
        )
        store._insert_ledger_record_locked(conn, "expression", EXPR_ID)
        conn.execute(
            "INSERT INTO proposals "
            "(id, document_id, base_content_sha256, selector_json, quote_exact, "
            "span_sha256, replacement, rationale, tldr, claim_refs_json, "
            "canonical_sha256, dedup_key, expires_at, created_at, "
            "created_by_kind, created_by_ref, meta_json, redacted_at) "
            "VALUES (?, ?, ?, ?, 'old text', ?, 'new text', 'reason', 'tldr', "
            "?, ?, ?, NULL, ?, 'agent_run', 'run-1', ?, NULL)",
            (
                PROP_ID,
                DOC_ID,
                CONTENT_SHA,
                json.dumps({"exact": "old text"}),
                PROP_SPAN_SHA,
                json.dumps([{"claim": CLAIM_REF, "role": "instantiation"}]),
                PROP_CANON,
                PROP_DEDUP,
                NOW,
                json.dumps({"producer": {"harness": "test"}}),
            ),
        )
        store._insert_ledger_record_locked(conn, "proposal", PROP_ID)
        conn.execute(
            "INSERT INTO proposal_status_events "
            "(id, proposal_id, status, decision, at, actor_kind, actor_ref, "
            "basis_kind, basis_ref, note) VALUES (?, ?, 'open', NULL, ?, "
            "'system', NULL, 'rule', 'created', NULL)",
            (PSE_ID, PROP_ID, NOW),
        )
        store._insert_ledger_record_locked(conn, "proposal_status_event", PSE_ID)
        conn.execute(
            "INSERT INTO doc_events "
            "(id, document_id, kind, at, actor_kind, actor_ref, content_sha256, "
            "ydoc_snapshot_sha256, detail) VALUES (?, ?, 'registered', ?, "
            "'human', 'user-1', ?, NULL, NULL)",
            (DE_REG_ID, DOC_ID, NOW, CONTENT_SHA),
        )
        store._insert_ledger_record_locked(conn, "doc_event", DE_REG_ID)
        conn.execute(
            "INSERT INTO doc_events "
            "(id, document_id, kind, at, actor_kind, actor_ref, content_sha256, "
            "ydoc_snapshot_sha256, detail) VALUES (?, ?, 'materialized', ?, "
            "'human', 'user-1', ?, ?, NULL)",
            (DE_MAT_ID, DOC_ID, NOW, CONTENT_SHA, ydoc_snapshot_sha256),
        )
        store._insert_ledger_record_locked(conn, "doc_event", DE_MAT_ID)


def _objects(payload: bytes) -> list[dict[str, Any]]:
    return [json.loads(line) for line in payload.decode("utf-8").splitlines()]


def _table_rows(store: TruthStore, table: str, order: str) -> list[dict[str, Any]]:
    conn = store.connect()
    try:
        return [
            dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")
        ]
    finally:
        conn.close()


DOC_TABLES = {
    "documents": "id",
    "document_spans": "id",
    "expressions": "id",
    "proposals": "id",
    "proposal_status_events": "seq",
    "doc_events": "id",
}


def test_document_surface_round_trips_lossless_including_ydoc_blob(
    tmp_path: Path,
) -> None:
    source = _create_store(tmp_path / "source", store_id="b0" * 16)
    _write_blob(source, SNAPSHOT_SHA, SNAPSHOT_BYTES)
    _insert_document(source)

    exported = export_store(source)
    objects = _objects(exported.path.read_bytes())
    assert objects[0]["format_version"] == FORMAT_VERSION == 3

    record_types = {
        item["record_type"]
        for item in objects
        if item["record_type"] not in {"header", "blob", "end"}
    }
    assert {
        "document",
        "document_span",
        "expression",
        "proposal",
        "proposal_status_event",
        "doc_event",
    } <= record_types

    blob_records = [item for item in objects if item["record_type"] == "blob"]
    assert [item["content_sha256"] for item in blob_records] == [SNAPSHOT_SHA]

    target = tmp_path / "target"
    target.mkdir()
    registry = FakeRegistry()
    imported = import_store(exported.path, target, registry=registry)
    restored = imported.store

    assert imported.source_format_version == 3
    assert restored.store_id == source.store_id
    for table, order in DOC_TABLES.items():
        assert _table_rows(restored, table, order) == _table_rows(source, table, order)

    assert (restored.paths.blobs / SNAPSHOT_SHA).read_bytes() == SNAPSHOT_BYTES
    reexport = export_store(restored, tmp_path / "restored.jsonl")
    assert reexport.path.read_bytes() == exported.path.read_bytes()


def test_redacted_proposal_round_trips_with_hashes_retained(
    tmp_path: Path,
) -> None:
    source = _create_store(tmp_path / "source", store_id="b1" * 16)
    _write_blob(source, SNAPSHOT_SHA, SNAPSHOT_BYTES)
    _insert_document(source)
    # A standing policy scrubs an expired proposal's content (the anti-anchoring
    # shape) and appends the audit companion. This is the engine's policy-basis
    # proposal redaction: reason expired_content, basis the exact standing-policy
    # key, prior proposal status expired. The staged-import integrity gate must
    # validate it cleanly (redactable map plus the proposal policy branch).
    expired_pse_id = "c0" * 16
    redaction_id = "c1" * 16
    with source.write_transaction() as conn:
        conn.execute(
            "INSERT INTO proposal_status_events "
            "(id, proposal_id, status, decision, at, actor_kind, actor_ref, "
            "basis_kind, basis_ref, note) VALUES (?, ?, 'expired', NULL, ?, "
            "'system', NULL, 'rule', 'proposal_max_age:7200', NULL)",
            (expired_pse_id, PROP_ID, NOW),
        )
        source._insert_ledger_record_locked(
            conn, "proposal_status_event", expired_pse_id
        )
        conn.execute(
            "UPDATE proposals SET quote_exact = NULL, replacement = NULL, "
            "rationale = NULL, tldr = NULL, claim_refs_json = NULL, "
            "selector_json = ?, redacted_at = ? WHERE id = ?",
            (REDACTED_SELECTOR_JSON, NOW, PROP_ID),
        )
        conn.execute(
            "INSERT INTO redaction_events "
            "(id, subject_kind, subject_ref, at, actor_ref, basis_kind, "
            "basis_ref, reason) VALUES (?, 'proposal', ?, ?, 'user-1', "
            "'policy', ?, 'expired_content')",
            (
                redaction_id,
                PROP_ID,
                NOW,
                policy_basis_ref(source, "expired_content"),
            ),
        )
        source._insert_ledger_record_locked(conn, "redaction_event", redaction_id)

    exported = export_store(source)
    target = tmp_path / "target"
    target.mkdir()
    restored = import_store(exported.path, target, registry=FakeRegistry()).store

    row = _table_rows(restored, "proposals", "id")[0]
    assert row["quote_exact"] is None
    assert row["replacement"] is None
    assert row["selector_json"] == REDACTED_SELECTOR_JSON
    assert row["redacted_at"] == NOW
    # Ids and every hash survive redaction so gesture bindings and suppression
    # memory remain intact.
    assert row["canonical_sha256"] == PROP_CANON
    assert row["dedup_key"] == PROP_DEDUP
    assert row["base_content_sha256"] == CONTENT_SHA
    reexport = export_store(restored, tmp_path / "again.jsonl")
    assert reexport.path.read_bytes() == exported.path.read_bytes()


def test_export_refuses_a_missing_ydoc_snapshot_blob(tmp_path: Path) -> None:
    source = _create_store(tmp_path / "source", store_id="b2" * 16)
    _write_blob(source, SNAPSHOT_SHA, SNAPSHOT_BYTES)
    _insert_document(source)
    (source.paths.blobs / SNAPSHOT_SHA).unlink()

    with pytest.raises(TruthExportError, match="ydoc snapshot blob is unavailable"):
        export_store(source)


def test_ydoc_and_evidence_blob_share_one_content_address(
    tmp_path: Path,
) -> None:
    source = _create_store(tmp_path / "source", store_id="b3" * 16)
    # An evidence blob and a ydoc snapshot with identical bytes dedup to one
    # content-addressed blob carrying two references.
    shared = b"shared-content-addressed-bytes"
    shared_digest = sha256_bytes(shared)
    source.capture_evidence(
        kind="artifact",
        source_locator="file:///artifact.bin",
        actor=HUMAN,
        acquisition_method="paste",
        content=shared,
        media_type="application/octet-stream",
        record_id="d0" * 16,
        acquired_at=NOW,
        created_at=NOW,
    )
    _write_blob(source, shared_digest, shared)
    _insert_document(source, ydoc_snapshot_sha256=shared_digest)

    objects = _objects(export_store(source).path.read_bytes())
    blob_records = [item for item in objects if item["record_type"] == "blob"]
    assert [item["content_sha256"] for item in blob_records] == [shared_digest]

    target = tmp_path / "target"
    target.mkdir()
    restored = import_store(
        export_store(source).path, target, registry=FakeRegistry()
    ).store
    assert (restored.paths.blobs / shared_digest).read_bytes() == shared


def _seeded_objects(tmp_path: Path) -> list[dict[str, Any]]:
    source = _create_store(tmp_path / "src", store_id="b4" * 16)
    _write_blob(source, SNAPSHOT_SHA, SNAPSHOT_BYTES)
    _insert_document(source)
    return _objects(export_store(source).path.read_bytes())


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


def _repack(objects: list[dict[str, Any]]) -> bytes:
    prefix = b"".join(_canonical_line(item) for item in objects[:-1])
    footer = objects[-1]
    data = [
        item
        for item in objects
        if item["record_type"] not in {"header", "blob", "end"}
    ]
    footer["record_count"] = len(data)
    footer["blob_count"] = sum(item["record_type"] == "blob" for item in objects)
    footer["last_seq"] = data[-1]["seq"] if data else 0
    footer["stream_sha256"] = sha256_bytes(prefix)
    return prefix + _canonical_line(footer)


@pytest.mark.parametrize(
    ("record_type", "field", "value", "message"),
    [
        ("expression", "role", "editorial", "expression role"),
        ("expression", "claim_ref_kind", "guess", "claim_ref_kind"),
        ("proposal_status_event", "status", "half-open", "invalid status"),
        ("proposal_status_event", "decision", "approve", "invalid decision"),
        ("doc_event", "kind", "teleported", "invalid kind"),
        ("document", "content_sha256", "not-a-digest", "content_sha256"),
    ],
)
def test_import_rejects_malformed_document_records(
    tmp_path: Path,
    record_type: str,
    field: str,
    value: str,
    message: str,
) -> None:
    objects = _seeded_objects(tmp_path)
    target = next(
        item
        for item in objects
        if item["record_type"] == record_type
    )
    target["record"][field] = value
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    with pytest.raises(TruthImportError, match=message):
        import_store(_repack(objects), target_dir, registry=FakeRegistry())
    assert list(target_dir.iterdir()) == []


def test_import_rejects_a_document_dangling_reference(tmp_path: Path) -> None:
    objects = _seeded_objects(tmp_path)
    proposal = next(
        item for item in objects if item["record_type"] == "proposal"
    )
    proposal["record"]["document_id"] = "ff" * 16
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    with pytest.raises(TruthImportError, match="proposal.document_id"):
        import_store(_repack(objects), target_dir, registry=FakeRegistry())
    assert list(target_dir.iterdir()) == []
