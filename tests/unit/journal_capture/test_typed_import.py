from __future__ import annotations

import hashlib
from datetime import date

import pytest

from work_buddy.journal_capture.authority import JournalAuthorityCoordinator
from work_buddy.journal_capture.content_adapter import JournalContentAdapter
from work_buddy.journal_capture.dispatch import JournalSourceDispatcher
from work_buddy.journal_capture.domain import JournalDomainService
from work_buddy.journal_capture.configuration import JournalProfileConfigurationService
from work_buddy.journal_capture.import_cohort import (
    JournalImportCohortDrift,
    LegacyJournalImportService,
)
from work_buddy.journal_capture.partition import JournalPartition
from work_buddy.journal_capture.native_ops import _dates_descending
from work_buddy.journal_capture.service import JournalCaptureService
from work_buddy.journal_capture.typed_import import (
    JournalImportFieldMapping,
    JournalImportProfileMapping,
    JournalImportProfileModuleRef,
    JournalImportTypedObservation,
)
from work_buddy.sources import SourceRef, redact_source
from work_buddy.truth.registry import TruthStoreRegistry

from .test_import_cohort import _context, _mapping, _pause_cutover, _stores


def _typed_mapping() -> JournalImportProfileMapping:
    return JournalImportProfileMapping(
        mapping_version="test-private-history/v1",
        profile_id="test.private.history",
        profile_revision=1,
        profile_name="Private historical profile",
        module_instance_id="test.private.markers",
        module_instance_version=1,
        module_label="Historical markers",
        module_slot_id="historical-markers",
        day_timezone="America/New_York",
        day_boundary="05:00",
        boundary_policy_revision="test-private-history-policy/v1",
        profile_modules=(
            JournalImportProfileModuleRef("capture", 0, "simple.capture", 1),
            JournalImportProfileModuleRef("day-stream", 1, "simple.stream", 1),
            JournalImportProfileModuleRef("notes", 2, "simple.notes", 1),
            JournalImportProfileModuleRef(
                "historical-markers", 3, "test.private.markers", 1
            ),
        ),
        fields=(
            JournalImportFieldMapping(
                field_id="test.observed-at",
                definition_version=1,
                owner="test-private-import",
                stable_key="observed_at",
                label="Observed at",
                value_kind="instant",
                search_mode="lexical",
            ),
        ),
    )


def _source_day(root):
    value = b"2026-08-20T08:00:00-04:00"
    raw = (
        b"---\r\nobserved_at: "
        + value
        + b"\r\n---\r\n# **Log**\r\n* one retained history item\r\n"
    )
    path = root / "2026-08-20.md"
    path.write_bytes(raw)
    start = raw.index(value)
    return path, raw, value, start


def _observation(value: bytes, start: int) -> JournalImportTypedObservation:
    return JournalImportTypedObservation(
        relative_path="2026-08-20.md",
        local_date="2026-08-20",
        field_id="test.observed-at",
        evidence_start_byte=start,
        evidence_end_byte=start + len(value),
        evidence_sha256=hashlib.sha256(value).hexdigest(),
        extractor_receipt_sha256=hashlib.sha256(
            b"test-frontmatter-extractor/v1"
        ).hexdigest(),
        value=value.decode("utf-8"),
    )


def test_typed_profile_and_observation_seal_atomically_but_stay_inactive(tmp_path):
    source_root = tmp_path / "frozen"
    source_root.mkdir()
    _, _, value, start = _source_day(source_root)
    journal, sources = _stores(tmp_path)
    importer = LegacyJournalImportService(journal, sources)
    domain = JournalDomainService(journal)
    configuration = JournalProfileConfigurationService(journal)
    partition = JournalPartition(journal)

    cohort = importer.prepare(
        source_root,
        mapping=_mapping(),
        client_mutation_id="typed-import-prepare-0001",
        actor={"kind": "migration_operator", "id": "test"},
    )
    mapping = _typed_mapping()
    assert importer.prepare_typed_mapping(cohort.cohort_id, mapping) == mapping.sha256
    importer.stage(cohort.cohort_id, source_root, ingress_context=_context())
    observation = _observation(value, start)
    assert importer.stage_typed_observations(cohort.cohort_id, (observation,)) == 1
    assert importer.stage_typed_observations(cohort.cohort_id, (observation,)) == 1
    verified = importer.verify(cohort.cohort_id, source_root)
    assert verified.expected_observation_count == 1

    _pause_cutover(journal, cohort.cohort_id)
    sealed = importer.seal(cohort.cohort_id, source_root)
    replay = importer.seal(cohort.cohort_id, source_root)
    assert replay == sealed
    assert domain.list_field_values("2026-08-20") == ()
    assert all(profile.profile_id != mapping.profile_id for profile in domain.list_profiles())
    assert all(
        profile["profileId"] != mapping.profile_id
        for profile in configuration.catalog()["profiles"]
    )
    assert tuple(
        _dates_descending(
            journal, lower=date(2026, 8, 20), upper=date(2026, 8, 20)
        )
    ) == ()
    inactive_composition = domain.resolve_day(
        local_date="2026-08-20",
        timezone="America/New_York",
        boundary="05:00",
        window_start="2026-08-20T05:00:00-04:00",
        window_end="2026-08-21T05:00:00-04:00",
    )
    assert inactive_composition.profile.profile_id == "simple-journal"
    assert inactive_composition.persisted is False
    assert all(not ref.item_id.startswith("field:") for ref in partition.discover())
    with journal._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_field_values WHERE import_cohort_id=?",
            (cohort.cohort_id,),
        ).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM journal_captures").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 0
        assert conn.execute(
            "SELECT state FROM journal_import_typed_observations"
        ).fetchone()[0] == "materialized"
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_day_composition_snapshots "
            "WHERE import_cohort_id=?",
            (cohort.cohort_id,),
        ).fetchone()[0] == 1

    JournalAuthorityCoordinator(journal).activate_database_only(
        cohort_id=cohort.cohort_id,
        client_mutation_id="typed-import-activate-0001",
        actor={"kind": "migration_operator", "id": "test"},
    )
    field = domain.list_field_values("2026-08-20")[0]
    assert field.value == "2026-08-20T08:00:00-04:00"
    assert field.authorship == "unknown"
    assert field.review_state == "unknown"
    assert any(profile.profile_id == mapping.profile_id for profile in domain.list_profiles())
    assert any(
        profile["profileId"] == mapping.profile_id
        for profile in configuration.catalog()["profiles"]
    )
    assert any(ref.item_id.startswith("field:") for ref in partition.discover())
    assert tuple(
        _dates_descending(
            journal, lower=date(2026, 8, 20), upper=date(2026, 8, 20)
        )
    ) == (date(2026, 8, 20),)
    composition = domain.resolve_day(
        local_date="2026-08-20",
        timezone="America/New_York",
        boundary="05:00",
        window_start="2026-08-20T05:00:00-04:00",
        window_end="2026-08-21T05:00:00-04:00",
    )
    assert composition.persisted is True
    assert composition.profile.profile_id == mapping.profile_id
    assert [item.slot_id for item in composition.modules] == [
        "capture", "day-stream", "notes", "historical-markers"
    ]
    assert composition.fields[0].composition_slot_id == (
        "historical-markers:field:test.observed-at"
    )
    assert field.composition_slot_id == composition.fields[0].composition_slot_id
    with journal._connect() as conn:
        assert tuple(
            conn.execute(
                "SELECT authorship,review_state FROM journal_field_value_revisions"
            ).fetchone()
        ) == ("unknown", "unknown")
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_search_outbox "
            "WHERE aggregate_type='field_value' AND visibility_cohort_id=?",
            (cohort.cohort_id,),
        ).fetchone()[0] == 1


def test_import_file_redaction_also_scrubs_typed_observation_history(tmp_path):
    source_root = tmp_path / "frozen"
    source_root.mkdir()
    _, _, value, start = _source_day(source_root)
    journal, sources = _stores(tmp_path)
    context = _context()
    importer = LegacyJournalImportService(journal, sources)
    cohort = importer.prepare(
        source_root,
        mapping=_mapping(),
        client_mutation_id="typed-import-redaction-prepare-0001",
        actor={"kind": "migration_operator", "id": "test"},
    )
    importer.prepare_typed_mapping(cohort.cohort_id, _typed_mapping())
    importer.stage(cohort.cohort_id, source_root, ingress_context=context)
    importer.stage_typed_observations(cohort.cohort_id, (_observation(value, start),))
    importer.verify(cohort.cohort_id, source_root)
    _pause_cutover(journal, cohort.cohort_id)
    importer.seal(cohort.cohort_id, source_root)
    JournalAuthorityCoordinator(journal).activate_database_only(
        cohort_id=cohort.cohort_id,
        client_mutation_id="typed-import-redaction-activate-0001",
        actor={"kind": "migration_operator", "id": "test"},
    )

    with journal._connect() as conn:
        file_row = conn.execute(
            "SELECT source_ref FROM journal_import_files WHERE cohort_id=?",
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
    dispatcher = JournalSourceDispatcher(
        sources,
        JournalCaptureService(journal, JournalContentAdapter(tmp_path / "vault")),
        service_principal=context.service_principal,
        document_registry=TruthStoreRegistry(tmp_path / "truth-registry.db"),
    )
    assert redaction.pending_effect_ids
    assert dispatcher.drain().delivered == 1

    assert JournalDomainService(journal).list_field_values("2026-08-20") == ()
    with journal._connect() as conn:
        assert [
            row[0]
            for row in conn.execute(
                "SELECT value_json FROM journal_field_value_revisions ORDER BY revision"
            ).fetchall()
        ] == ['{"redacted":true}', '{"redacted":true}']
        receipt = conn.execute(
            "SELECT scrubbed_field_value_count FROM journal_import_source_redactions"
        ).fetchone()
        assert receipt[0] == 1
    with sources.connect() as conn:
        assert conn.execute(
            "SELECT status FROM source_usage_intents"
        ).fetchone()[0] == "released"


def test_typed_evidence_drift_aborts_without_native_publication(tmp_path):
    source_root = tmp_path / "frozen"
    source_root.mkdir()
    _, _, value, start = _source_day(source_root)
    journal, sources = _stores(tmp_path)
    importer = LegacyJournalImportService(journal, sources)
    cohort = importer.prepare(
        source_root,
        mapping=_mapping(),
        client_mutation_id="typed-import-drift-prepare-0001",
        actor={"kind": "migration_operator", "id": "test"},
    )
    importer.prepare_typed_mapping(cohort.cohort_id, _typed_mapping())
    importer.stage(cohort.cohort_id, source_root, ingress_context=_context())
    good = _observation(value, start)
    drifted = JournalImportTypedObservation(
        relative_path=good.relative_path,
        local_date=good.local_date,
        field_id=good.field_id,
        evidence_start_byte=good.evidence_start_byte,
        evidence_end_byte=good.evidence_end_byte,
        evidence_sha256="0" * 64,
        extractor_receipt_sha256=good.extractor_receipt_sha256,
        value=good.value,
    )

    with pytest.raises(JournalImportCohortDrift):
        importer.stage_typed_observations(cohort.cohort_id, (drifted,))

    assert importer.get(cohort.cohort_id).state == "aborted"
    with journal._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM journal_field_values").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM journal_items").fetchone()[0] == 0
    with sources.connect() as conn:
        assert conn.execute(
            "SELECT status FROM source_usage_intents"
        ).fetchone()[0] == "released"
