"""Deterministic recovery export tests for targeted truth stores."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import work_buddy.truth.export as truth_export
import work_buddy.truth.migrations as truth_migrations
from work_buddy.storage.migrations import Migration
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
from work_buddy.truth.identity import canonical_json, new_id, sha256_bytes
from work_buddy.truth.store import GestureRecord, PostCommitHookError, TruthStore


NOW = "2026-07-14T16:00:00.000+00:00"
LATER = "2026-07-14T16:01:00.000+00:00"
HUMAN = Actor("human", "user-1")

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


def _synthetic_v2(conn) -> None:
    conn.execute("CREATE TABLE import_v2_marker (id TEXT PRIMARY KEY)")


def _v2_runner() -> truth_migrations._TruthMigrationRunner:
    return truth_migrations._TruthMigrationRunner(
        "truth",
        migrations=[
            Migration(
                1,
                "initial truth ledger schema",
                truth_migrations._m001_initial_schema,
            ),
            Migration(2, "synthetic import v2", _synthetic_v2),
        ],
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

    gesture = GestureRecord(
        id=GESTURE_ID,
        at=LATER,
        surface="dashboard",
        actor_ref="user-1",
        kind="redact",
        subject_ref=derived.id,
        payload_sha256=derived.canonical_sha256,
        payload_excerpt=derived.proposition,
        context_sha256=None,
        expires_at=None,
        consumed_at=LATER,
    )
    with store.write_transaction() as conn:
        store._insert_gesture_locked(conn, gesture)
        conn.execute(
            "UPDATE claims SET proposition = '[redacted]', structured_json = NULL, "
            "redacted_at = ? WHERE id = ?",
            (LATER, derived.id),
        )
        conn.execute(
            "INSERT INTO redaction_events "
            "(id, subject_kind, subject_ref, at, actor_ref, basis_kind, "
            "basis_ref, reason) VALUES (?, 'claim', ?, ?, 'user-1', "
            "'gesture', ?, 'privacy')",
            (REDACTION_ID, derived.id, LATER, GESTURE_ID),
        )
        store._insert_ledger_record_locked(conn, "redaction_event", REDACTION_ID)
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
    (target / ".wb-truth").mkdir()

    result = import_store(exported.path, target, registry=FakeRegistry())

    assert result.record_count == 0
    assert result.blob_count == 0
    assert result.store.store_id == source.store_id
    again = export_store(result.store, tmp_path / "again.jsonl")
    assert again.path.read_bytes() == exported.path.read_bytes()


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
    assert not (target / ".wb-truth").exists()

    sidecar = target / ".wb-truth"
    sidecar.mkdir()
    (sidecar / "sentinel").write_text("keep", encoding="utf-8")
    with pytest.raises(TruthImportError, match="must be empty"):
        import_store(exported.path, target, registry=FakeRegistry())
    assert (sidecar / "sentinel").read_text(encoding="utf-8") == "keep"


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
        b'"format_version":2',
        b'"format_version":2,"format_version":2',
        1,
    )
    with pytest.raises(TruthImportError, match="malformed JSON"):
        import_store(duplicate_header, target, registry=FakeRegistry())
    assert list(target.iterdir()) == []


def test_older_schema_export_rebuilds_under_a_newer_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _create_store(tmp_path / "v1-source")
    claim = source.propose_claim(
        proposition="Portable history outlives the SQLite schema",
        claim_kind="fact",
        actor=HUMAN,
        created_at=NOW,
        status_at=NOW,
    ).claim
    v1_payload = source.paths.claims_export.read_bytes()
    assert _objects(v1_payload)[0]["store_info"]["schema_version"] == 1

    monkeypatch.setattr(truth_migrations, "TRUTH_MIGRATIONS", _v2_runner())
    monkeypatch.setattr(truth_export, "SCHEMA_VERSION", 2)
    target = tmp_path / "v2-target"
    target.mkdir()

    restored = import_store(v1_payload, target, registry=FakeRegistry()).store

    assert restored.get_claim(claim.id).canonical_sha256 == claim.canonical_sha256
    with restored.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'import_v2_marker'"
        ).fetchone()
    header = _objects(restored.paths.claims_export.read_bytes())[0]
    assert header["store_info"]["schema_version"] == 2


def test_staging_failure_is_not_published_and_existing_empty_target_is_restored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _create_store(tmp_path / "source")
    exported = export_store(source)
    target = tmp_path / "target"
    target.mkdir()
    sidecar = target / ".wb-truth"
    sidecar.mkdir()

    def fail_insert(*args, **kwargs):
        raise RuntimeError("staged insert failed")

    monkeypatch.setattr("work_buddy.truth.export._insert_records", fail_insert)
    with pytest.raises(RuntimeError, match="staged insert failed"):
        import_store(exported.path, target, registry=FakeRegistry())

    assert sidecar.is_dir()
    assert list(sidecar.iterdir()) == []
    assert not list(target.glob(".wb-truth-import-*"))


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
