from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3

import pytest

from work_buddy.journal_capture.domain import JournalDomainService
from work_buddy.journal_capture.authority import (
    JournalAuthorityCoordinator,
    JournalAuthorityStateError,
)
from work_buddy.journal_capture.content_adapter import JournalContentAdapter
from work_buddy.journal_capture.dispatch import JournalSourceDispatcher
from work_buddy.journal_capture.import_cohort import (
    JournalImportCohortDrift,
    JournalImportTarget,
    LegacyJournalImportMapping,
    LegacyJournalImportService,
)
from work_buddy.journal_capture.partition import JournalPartition
from work_buddy.journal_capture.store import JournalCaptureStore
from work_buddy.journal_capture.service import JournalCaptureService
from work_buddy.security.actors import ActorRef
from work_buddy.sources import SourceRef, SourceStore, TrustedIngressContext, redact_source
from work_buddy.truth.registry import TruthStoreRegistry


def _mapping() -> LegacyJournalImportMapping:
    return LegacyJournalImportMapping(
        mapping_version="test-journal-mapping/v1",
        targets={
            "log_section": JournalImportTarget(
                item_kind="record",
                module_instance_id="simple.stream",
                module_instance_version=1,
            ),
            "running_notes_section": JournalImportTarget(
                item_kind="running_note",
                module_instance_id="simple.notes",
                module_instance_version=1,
            ),
        },
    )


def _context() -> TrustedIngressContext:
    tenant = "tenant-journal-history-test"
    issuer = ActorRef("test-authority", "trusted-journal-import", "service", tenant)
    human = ActorRef("test-authority", "historical-profile", "human", tenant)
    service = ActorRef("test-authority", "journal-domain", "service", tenant)
    return TrustedIngressContext(
        issuer=issuer,
        issuer_version="test/v1",
        inputter=human,
        service_principal=service,
        tenant_scope_id=tenant,
        surface="journal-history-import",
        namespace="journal-history-import-staging",
        sensitivity_class="private",
        retention_class="durable",
        inputter_assurance="historical_inputter_only",
        authorization_fingerprint="a" * 64,
        permitted_purposes=("journal.history_import",),
    )


def _stores(tmp_path: Path) -> tuple[JournalCaptureStore, SourceStore]:
    return (
        JournalCaptureStore(tmp_path / "journal.db"),
        SourceStore.create(tmp_path / "staged-sources"),
    )


def _pause_cutover(journal: JournalCaptureStore, cohort_id: str) -> None:
    JournalAuthorityCoordinator(journal).pause_legacy_ingress(
        cohort_id=cohort_id,
        client_mutation_id=f"test-cutover-pause:{cohort_id}",
        actor={"kind": "migration_operator", "id": "test"},
    )


def _legacy_day(root: Path, *, day: str = "2026-08-20") -> tuple[Path, bytes]:
    raw = (
        b"---\r\ncontext_anchor: 08:00\r\n---\r\n"
        b"# **Log**\r\n* 9:00 AM - exact historical log\r\n"
        b"# **Running Notes / Considerations**\r\n"
        b"A private historical note with trailing spaces.  \r\n"
    )
    path = root / f"{day}.md"
    path.write_bytes(raw)
    return path, raw


def _counts(store: JournalCaptureStore) -> dict[str, int]:
    with store._connect() as conn:
        names = (
            "journal_items",
            "journal_item_revisions",
            "journal_search_outbox",
            "journal_import_receipts",
        )
        return {
            name: int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
            for name in names
        }


def test_staging_retains_every_original_byte_but_is_invisible_until_seal(tmp_path: Path):
    source_root = tmp_path / "frozen"
    source_root.mkdir()
    source_path, raw = _legacy_day(source_root)
    original_mtime = source_path.stat().st_mtime_ns
    journal, sources = _stores(tmp_path)
    service = LegacyJournalImportService(journal, sources)
    domain = JournalDomainService(journal)
    partition = JournalPartition(journal)

    cohort = service.prepare(
        source_root,
        mapping=_mapping(),
        client_mutation_id="journal-import-test-prepare-0001",
        actor={"kind": "migration_operator", "id": "test"},
    )

    assert cohort.state == "prepared"
    assert cohort.expected_byte_count == len(raw)
    assert cohort.expected_item_count == 2
    assert domain.list_native_items("2026-08-20") == ()
    assert list(partition.discover()) == []
    assert _counts(journal)["journal_search_outbox"] == 0
    with sources.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_items").fetchone()[0] == 0

    staged = service.stage(cohort.cohort_id, source_root, ingress_context=_context())

    assert staged.state == "staging"
    assert domain.list_native_items("2026-08-20") == ()
    assert list(partition.discover()) == []
    with sources.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_items").fetchone()[0] == 1
        representation = conn.execute(
            "SELECT inline_content,content_sha256,byte_length FROM source_representations"
        ).fetchone()
        assert bytes(representation["inline_content"]) == raw
        assert representation["content_sha256"] == hashlib.sha256(raw).hexdigest()
        assert representation["byte_length"] == len(raw)
        author = conn.execute(
            "SELECT attribution_state FROM source_attributions WHERE role='author'"
        ).fetchone()
        assert author["attribution_state"] == "unknown"
    with journal._connect() as conn:
        file_row = conn.execute("SELECT * FROM journal_import_files").fetchone()
        assert file_row["source_usage_id"]
        assert file_row["source_usage_consumer_id"].startswith("journal-import-file:")
        assert file_row["source_usage_state"] == "acknowledged"
        spans = conn.execute(
            "SELECT * FROM journal_import_spans ORDER BY start_byte"
        ).fetchall()
        cursor = 0
        for span in spans:
            assert span["start_byte"] == cursor
            assert hashlib.sha256(raw[cursor : span["end_byte"]]).hexdigest() == span[
                "raw_sha256"
            ]
            cursor = span["end_byte"]
        assert cursor == file_row["byte_length"] == len(raw)
        assert sum(span["end_byte"] - span["start_byte"] for span in spans) == len(raw)
        assert all(span["authorship"] == "unknown" for span in spans)
        assert all(span["review_state"] == "unknown" for span in spans)
    with sources.connect() as conn:
        usage = conn.execute(
            "SELECT status,purpose,consumer_domain,use_kind,disclosure_kind,"
            "redaction_policy FROM source_usage_intents"
        ).fetchone()
        assert tuple(usage) == (
            "acknowledged",
            "journal.history_import",
            "journal",
            "journal_history_import",
            "exact_readable_copy",
            "scrub",
        )

    verified = service.verify(cohort.cohort_id, source_root)
    assert verified.state == "verified"
    assert domain.list_native_items("2026-08-20") == ()
    assert _counts(journal)["journal_search_outbox"] == 0

    _pause_cutover(journal, cohort.cohort_id)
    sealed = service.seal(cohort.cohort_id, source_root)

    assert sealed.state == "sealed"
    assert domain.list_native_items("2026-08-20") == ()
    assert domain.pending_search_events() == ()
    assert list(partition.discover()) == []
    with journal._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM journal_items").fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_search_outbox"
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_items WHERE import_cohort_id=?",
            (cohort.cohort_id,),
        ).fetchone()[0] == 2

    authority = JournalAuthorityCoordinator(journal).activate_database_only(
        cohort_id=cohort.cohort_id,
        client_mutation_id="journal-import-test-authority-activate-0001",
        actor={"kind": "migration_operator", "id": "test"},
    )
    assert authority.mode == "database_only"
    items = domain.list_native_items("2026-08-20")
    by_kind = {item.item_kind: item for item in items}
    assert set(by_kind) == {"record", "running_note"}
    assert "exact historical log" in by_kind["record"].plain_value
    assert "private historical note" in by_kind["running_note"].plain_value
    assert len(domain.pending_search_events()) == 2
    discovered = list(partition.discover())
    assert len(discovered) == 2
    assert all(partition.parse(ref.item_id) for ref in discovered)
    with journal._connect() as conn:
        provenance = conn.execute(
            "SELECT authorship,review_state FROM journal_item_revisions ORDER BY item_id"
        ).fetchall()
        assert [(row["authorship"], row["review_state"]) for row in provenance] == [
            ("unknown", "unknown"),
            ("unknown", "unknown"),
        ]
    assert source_path.read_bytes() == raw
    assert source_path.stat().st_mtime_ns == original_mtime


def test_import_source_redaction_scrubs_every_native_copy_before_usage_release(
    tmp_path: Path,
):
    source_root = tmp_path / "frozen"
    source_root.mkdir()
    _legacy_day(source_root)
    journal, sources = _stores(tmp_path)
    context = _context()
    importer = LegacyJournalImportService(journal, sources)
    cohort = importer.prepare(
        source_root,
        mapping=_mapping(),
        client_mutation_id="journal-import-redaction-prepare-0001",
        actor={"kind": "migration_operator", "id": "test"},
    )
    importer.stage(cohort.cohort_id, source_root, ingress_context=context)
    importer.verify(cohort.cohort_id, source_root)
    _pause_cutover(journal, cohort.cohort_id)
    importer.seal(cohort.cohort_id, source_root)
    JournalAuthorityCoordinator(journal).activate_database_only(
        cohort_id=cohort.cohort_id,
        client_mutation_id="journal-import-redaction-activate-0001",
        actor={"kind": "migration_operator", "id": "test"},
    )
    domain = JournalDomainService(journal)
    assert len(domain.list_native_items("2026-08-20")) == 2

    with journal._connect() as conn:
        file_row = conn.execute(
            "SELECT source_ref,source_usage_id FROM journal_import_files "
            "WHERE cohort_id=?",
            (cohort.cohort_id,),
        ).fetchone()
    source_ref = SourceRef.parse(str(file_row["source_ref"]))
    sources.grant_access(
        source_ref=source_ref,
        principal=context.inputter,
        purpose="redaction",
        access_mode="metadata",
        authorization_fingerprint=context.authorization_fingerprint,
    )
    redaction = redact_source(
        sources,
        source_ref=source_ref,
        actor=context.inputter,
        authorization_fingerprint=context.authorization_fingerprint,
        reason_code="user_requested",
    )
    assert len(redaction.pending_effect_ids) == 1

    vault = tmp_path / "vault"
    vault.mkdir()
    dispatcher = JournalSourceDispatcher(
        sources,
        JournalCaptureService(journal, JournalContentAdapter(vault)),
        service_principal=context.service_principal,
        document_registry=TruthStoreRegistry(tmp_path / "truth-registry.db"),
    )
    assert dispatcher.drain().delivered == 1
    assert dispatcher.drain().delivered == 0

    assert domain.list_native_items("2026-08-20") == ()
    with journal._connect() as conn:
        assert {
            row[0]
            for row in conn.execute(
                "SELECT current_plain_value FROM journal_items "
                "WHERE import_cohort_id=?",
                (cohort.cohort_id,),
            )
        } == {"[redacted]"}
        assert {
            row[0]
            for row in conn.execute(
                "SELECT revision.plain_value FROM journal_item_revisions AS revision "
                "JOIN journal_items AS item ON item.item_id=revision.item_id "
                "WHERE item.import_cohort_id=?",
                (cohort.cohort_id,),
            )
        } == {"[redacted]"}
        receipt = conn.execute(
            "SELECT state,scrubbed_item_count FROM journal_import_source_redactions"
        ).fetchone()
        assert tuple(receipt) == ("committed", 2)
        dependency = conn.execute(
            "SELECT source_usage_state FROM journal_import_files WHERE cohort_id=?",
            (cohort.cohort_id,),
        ).fetchone()
        assert dependency[0] == "released"
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_search_outbox WHERE event_kind='delete'"
        ).fetchone()[0] == 2
        with pytest.raises(sqlite3.IntegrityError, match="is_immutable"):
            conn.execute(
                "UPDATE journal_item_revisions SET plain_value='restored' "
                "WHERE item_id IN (SELECT item_id FROM journal_items "
                "WHERE import_cohort_id=?)",
                (cohort.cohort_id,),
            )
    with sources.connect() as conn:
        usage = conn.execute(
            "SELECT status,maintenance_state FROM source_usage_intents WHERE usage_id=?",
            (file_row["source_usage_id"],),
        ).fetchone()
        assert tuple(usage) == ("released", "completed")
        event = conn.execute(
            "SELECT managed_copy_state FROM source_redaction_events "
            "WHERE redaction_event_id=?",
            (redaction.redaction_event_id,),
        ).fetchone()
        assert event[0] == "complete"


def test_authority_activation_rejects_unacknowledged_import_source_dependency(
    tmp_path: Path,
):
    source_root = tmp_path / "frozen"
    source_root.mkdir()
    _legacy_day(source_root)
    journal, sources = _stores(tmp_path)
    importer = LegacyJournalImportService(journal, sources)
    cohort = importer.prepare(
        source_root,
        mapping=_mapping(),
        client_mutation_id="journal-import-dependency-gate-prepare-0001",
        actor={"kind": "migration_operator", "id": "test"},
    )
    importer.stage(cohort.cohort_id, source_root, ingress_context=_context())
    importer.verify(cohort.cohort_id, source_root)
    _pause_cutover(journal, cohort.cohort_id)
    importer.seal(cohort.cohort_id, source_root)
    with journal.transaction() as conn:
        conn.execute(
            "UPDATE journal_import_files SET source_usage_state='released' "
            "WHERE cohort_id=?",
            (cohort.cohort_id,),
        )

    coordinator = JournalAuthorityCoordinator(journal)
    with pytest.raises(JournalAuthorityStateError, match="acknowledged import Source"):
        coordinator.activate_database_only(
            cohort_id=cohort.cohort_id,
            client_mutation_id="journal-import-dependency-gate-activate-0001",
            actor={"kind": "migration_operator", "id": "test"},
        )
    assert coordinator.state().mode == "legacy_compatibility"


def test_source_commit_crash_boundary_replays_without_duplicate_authority(tmp_path: Path):
    source_root = tmp_path / "frozen"
    source_root.mkdir()
    _legacy_day(source_root)
    journal, sources = _stores(tmp_path)
    calls = 0

    def crash_once(_cohort_id: str, _file_id: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated process loss after Source commit")

    service = LegacyJournalImportService(
        journal, sources, source_committed=crash_once
    )
    prepared = service.prepare(
        source_root,
        mapping=_mapping(),
        client_mutation_id="journal-import-crash-prepare-0001",
        actor={"kind": "migration_operator", "id": "test"},
    )

    with pytest.raises(RuntimeError, match="simulated process loss"):
        service.stage(prepared.cohort_id, source_root, ingress_context=_context())

    assert service.get(prepared.cohort_id).state == "staging"
    assert _counts(journal)["journal_items"] == 0
    with sources.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_items").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM ingress_submissions").fetchone()[0] == 1

    recovered = LegacyJournalImportService(journal, sources)
    assert recovered.stage(
        prepared.cohort_id, source_root, ingress_context=_context()
    ).state == "staging"
    with sources.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_items").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM ingress_submissions").fetchone()[0] == 1
    with journal._connect() as conn:
        progress = conn.execute(
            "SELECT state,attempts FROM journal_import_progress "
            "WHERE phase='stage_file'"
        ).fetchone()
        assert progress["state"] == "succeeded"
        assert progress["attempts"] == 2

    recovered.stage(prepared.cohort_id, source_root, ingress_context=_context())
    recovered.verify(prepared.cohort_id, source_root)
    recovered.verify(prepared.cohort_id, source_root)
    _pause_cutover(journal, prepared.cohort_id)
    first_seal = recovered.seal(prepared.cohort_id, source_root)
    second_seal = recovered.seal(prepared.cohort_id, source_root)

    assert second_seal == first_seal
    assert _counts(journal)["journal_items"] == 2
    assert _counts(journal)["journal_item_revisions"] == 2
    assert _counts(journal)["journal_search_outbox"] == 2
    with journal._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_import_receipts WHERE receipt_kind='file_staged'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_import_receipts WHERE receipt_kind='sealed'"
        ).fetchone()[0] == 1


def test_journal_stage_commit_crash_reconciles_source_acknowledgement(
    tmp_path: Path,
    monkeypatch,
):
    source_root = tmp_path / "frozen"
    source_root.mkdir()
    _legacy_day(source_root)
    journal, sources = _stores(tmp_path)
    importer = LegacyJournalImportService(journal, sources)
    cohort = importer.prepare(
        source_root,
        mapping=_mapping(),
        client_mutation_id="journal-import-ack-crash-prepare-0001",
        actor={"kind": "migration_operator", "id": "test"},
    )
    acknowledge = sources.acknowledge_usage
    calls = 0

    def crash_before_source_ack(usage_id: str, *, at: str | None = None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated process loss before Source acknowledgement")
        return acknowledge(usage_id, at=at)

    monkeypatch.setattr(sources, "acknowledge_usage", crash_before_source_ack)
    with pytest.raises(RuntimeError, match="before Source acknowledgement"):
        importer.stage(cohort.cohort_id, source_root, ingress_context=_context())

    with journal._connect() as conn:
        file_row = conn.execute(
            "SELECT state,source_usage_state,source_usage_id "
            "FROM journal_import_files WHERE cohort_id=?",
            (cohort.cohort_id,),
        ).fetchone()
        assert (file_row["state"], file_row["source_usage_state"]) == (
            "staged",
            "reserved",
        )
        usage_id = str(file_row["source_usage_id"])
    with sources.connect() as conn:
        assert conn.execute(
            "SELECT status FROM source_usage_intents WHERE usage_id=?", (usage_id,)
        ).fetchone()[0] == "reserved"

    monkeypatch.setattr(sources, "acknowledge_usage", acknowledge)
    recovered = LegacyJournalImportService(journal, sources)
    assert recovered.stage(
        cohort.cohort_id, source_root, ingress_context=_context()
    ).state == "staging"
    with journal._connect() as conn:
        assert conn.execute(
            "SELECT source_usage_state FROM journal_import_files WHERE cohort_id=?",
            (cohort.cohort_id,),
        ).fetchone()[0] == "acknowledged"
    with sources.connect() as conn:
        assert conn.execute(
            "SELECT status FROM source_usage_intents WHERE usage_id=?", (usage_id,)
        ).fetchone()[0] == "acknowledged"


def test_frozen_tree_drift_aborts_without_source_or_live_journal_writes(tmp_path: Path):
    source_root = tmp_path / "frozen"
    source_root.mkdir()
    source_path, _raw = _legacy_day(source_root)
    journal, sources = _stores(tmp_path)
    service = LegacyJournalImportService(journal, sources)
    prepared = service.prepare(
        source_root,
        mapping=_mapping(),
        client_mutation_id="journal-import-drift-prepare-0001",
        actor={"kind": "migration_operator", "id": "test"},
    )
    source_path.write_bytes(source_path.read_bytes() + b"changed after freeze\n")

    with pytest.raises(JournalImportCohortDrift, match="frozen Journal source changed"):
        service.stage(prepared.cohort_id, source_root, ingress_context=_context())

    aborted = service.get(prepared.cohort_id)
    assert aborted.state == "aborted"
    assert aborted.abort_code == "stage_source_drift"
    assert _counts(journal)["journal_items"] == 0
    assert _counts(journal)["journal_search_outbox"] == 0
    with sources.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_items").fetchone()[0] == 0


def test_drift_after_staging_never_publishes_the_retained_cohort(tmp_path: Path):
    source_root = tmp_path / "frozen"
    source_root.mkdir()
    source_path, _raw = _legacy_day(source_root)
    journal, sources = _stores(tmp_path)
    service = LegacyJournalImportService(journal, sources)
    prepared = service.prepare(
        source_root,
        mapping=_mapping(),
        client_mutation_id="journal-import-drift-prepare-0002",
        actor={"kind": "migration_operator", "id": "test"},
    )
    service.stage(prepared.cohort_id, source_root, ingress_context=_context())
    source_path.write_bytes(b"# **Log**\na replacement\n")

    with pytest.raises(JournalImportCohortDrift):
        service.verify(prepared.cohort_id, source_root)

    assert service.get(prepared.cohort_id).state == "aborted"
    assert _counts(journal)["journal_items"] == 0
    assert list(JournalPartition(journal).discover()) == []
    with sources.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_items").fetchone()[0] == 1


def test_prepare_replay_uses_deterministic_cohort_file_span_and_request_ids(tmp_path: Path):
    source_root = tmp_path / "frozen"
    source_root.mkdir()
    _legacy_day(source_root)
    journal, sources = _stores(tmp_path)
    service = LegacyJournalImportService(journal, sources)

    first = service.prepare(
        source_root,
        mapping=_mapping(),
        client_mutation_id="journal-import-replay-prepare-0001",
        actor={"kind": "migration_operator", "id": "test"},
    )
    second = service.prepare(
        source_root,
        mapping=_mapping(),
        client_mutation_id="journal-import-replay-prepare-0001",
        actor={"kind": "migration_operator", "id": "test"},
    )

    assert second == first
    with journal._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM journal_import_cohorts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM journal_import_files").fetchone()[0] == 1
        span_ids = [
            row[0]
            for row in conn.execute(
                "SELECT logical_id FROM journal_import_spans ORDER BY start_byte"
            ).fetchall()
        ]
        assert len(span_ids) == len(set(span_ids)) == first.expected_span_count
        receipt = conn.execute(
            "SELECT request_sha256,payload_json FROM journal_import_receipts "
            "WHERE receipt_kind='prepared'"
        ).fetchone()
        assert receipt["request_sha256"] == first.request_sha256
        assert "exact historical log" not in receipt["payload_json"]
