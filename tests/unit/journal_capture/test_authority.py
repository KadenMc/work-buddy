from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sqlite3
import threading

import pytest

from work_buddy.cutover_maintenance import (
    CutoverMaintenanceError,
    authorize_isolated_rehearsal_root,
)
from work_buddy.journal_capture.authority import (
    JournalAuthorityCoordinator,
    JournalAuthorityFenced,
    JournalAuthorityStateError,
    JournalCutoverPaused,
    legacy_markdown_write_guard,
)
from work_buddy.journal_capture.content_adapter import JournalContentAdapter
from work_buddy.journal_capture.dispatch import JournalSourceDispatcher
from work_buddy.journal_capture.domain import JournalDomainService
from work_buddy.journal_capture.import_cohort import (
    JournalImportCohortStateError,
    LegacyJournalImportMapping,
    LegacyJournalImportService,
)
from work_buddy.journal_capture.models import (
    CaptureMode,
    CaptureTarget,
    JournalCaptureConflict,
)
from work_buddy.journal_capture.ingress import (
    JournalCaptureIngress,
    JournalIngressQueued,
)
from work_buddy.journal_capture.partition import JournalPartition
from work_buddy.journal_capture.service import (
    CommittedIngress,
    JournalCaptureService,
    SmartCaptureResult,
)
from work_buddy.journal_capture.store import JournalCaptureStore
from work_buddy.security.actors import ActorRef
from work_buddy.settings import get_journal_day_window
from work_buddy.sources import SourceStore, TrustedIngressContext
from work_buddy.sources.dispatch import SourceOutbox


def _day_id(local_date: str = "2026-08-21") -> str:
    window = get_journal_day_window(local_date)
    return f"journal-day:{local_date}:{window.timezone}:{window.boundary}"


def _context() -> TrustedIngressContext:
    tenant = "tenant-journal-authority-test"
    issuer = ActorRef("test-authority", "trusted-import", "service", tenant)
    human = ActorRef("test-authority", "historical-profile", "human", tenant)
    service = ActorRef("test-authority", "journal-service", "service", tenant)
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
    )


def _rehearsal(store: JournalCaptureStore):
    return authorize_isolated_rehearsal_root(
        store.path.parent,
        authority_paths={"journal": store.path},
    )


def _seal_empty_cohort(
    tmp_path: Path, store: JournalCaptureStore
) -> tuple[str, SourceStore]:
    root = tmp_path / "frozen-history"
    root.mkdir()
    sources = SourceStore.create(tmp_path / "staged-sources")
    importer = LegacyJournalImportService(store, sources)
    prepared = importer.prepare(
        root,
        mapping=LegacyJournalImportMapping("empty-test/v1", {}),
        client_mutation_id="authority-empty-import-prepare-0001",
        actor={"kind": "migration_operator", "id": "test"},
    )
    importer.stage(prepared.cohort_id, root, ingress_context=_context())
    importer.verify(prepared.cohort_id, root)
    JournalAuthorityCoordinator(store).pause_legacy_ingress(
        cohort_id=prepared.cohort_id,
        client_mutation_id="authority-empty-import-pause-0001",
        actor={"kind": "migration_operator", "id": "test"},
    )
    importer.seal(prepared.cohort_id, root)
    return prepared.cohort_id, sources


def _activate(
    tmp_path: Path, store: JournalCaptureStore
) -> JournalAuthorityCoordinator:
    cohort_id, sources = _seal_empty_cohort(tmp_path, store)
    coordinator = JournalAuthorityCoordinator(store)
    state = coordinator.activate_database_only(
        cohort_id=cohort_id,
        client_mutation_id="authority-activate-0001",
        actor={"kind": "migration_operator", "id": "test"},
    )
    assert state.mode == "database_only"
    assert state.activated_cohort_id == cohort_id
    assert state.cutover_gate_state == "postseal_pending"
    drain_id = "authority-drain-0001"
    drained = JournalSourceDispatcher(
        sources,
        JournalCaptureService(store, JournalContentAdapter(tmp_path / "vault")),
        service_principal=_context().service_principal,
        worker_id="journal-authority-drain",
    ).drain_postseal_held(
        cohort_id=cohort_id,
        client_mutation_id=drain_id,
        actor={"kind": "migration_operator", "id": "test"},
    )
    assert drained["status"] == "drained"
    state = coordinator.release_postseal_ingress(
        cohort_id=cohort_id,
        client_mutation_id="authority-release-0001",
        actor={"kind": "migration_operator", "id": "test"},
        source_drain_mutation_id=drain_id,
        sources=sources,
        rehearsal_evidence_sha256s={
            "databaseCheckpoint": "a" * 64,
            "search": "b" * 64,
            "detachment": "c" * 64,
        },
        allow_unvalidated_rehearsal=True,
        rehearsal_authorization=_rehearsal(store),
    )
    assert state.cutover_gate_state == "open"
    return coordinator


def _ingress(suffix: str = "native") -> CommittedIngress:
    return CommittedIngress(
        source_ref=f"wb-source://authority/item-{suffix}",
        representation_id=f"representation-{suffix}",
        submission_id=f"submission-{suffix}",
        command_id=f"command-{suffix}",
        effect_id=f"effect-{suffix}",
        authorization_fingerprint="a" * 64,
        usage_id=f"usage-{suffix}",
    )


def test_preseal_pause_is_durable_idempotent_and_blocks_every_capture_or_file_write(
    tmp_path: Path,
):
    store = JournalCaptureStore(tmp_path / "journal.db")
    root = tmp_path / "frozen-history"
    root.mkdir()
    sources = SourceStore.create(tmp_path / "staged-sources")
    importer = LegacyJournalImportService(store, sources)
    prepared = importer.prepare(
        root,
        mapping=LegacyJournalImportMapping("empty-test/v1", {}),
        client_mutation_id="authority-pause-import-prepare-0001",
        actor={"kind": "migration_operator", "id": "test"},
    )
    importer.stage(prepared.cohort_id, root, ingress_context=_context())
    importer.verify(prepared.cohort_id, root)
    with pytest.raises(JournalImportCohortStateError, match="pause before seal"):
        importer.seal(prepared.cohort_id, root)

    coordinator = JournalAuthorityCoordinator(store)
    paused = coordinator.pause_legacy_ingress(
        cohort_id=prepared.cohort_id,
        client_mutation_id="authority-pause-0001",
        actor={"kind": "migration_operator", "id": "test"},
    )
    replay = coordinator.pause_legacy_ingress(
        cohort_id=prepared.cohort_id,
        client_mutation_id="authority-pause-0001",
        actor={"kind": "migration_operator", "id": "test"},
    )

    assert replay == paused
    assert paused.mode == "legacy_compatibility"
    assert paused.cutover_gate_state == "preseal_fenced"
    assert paused.capture_row_count == paused.capture_row_high_water == 0
    assert paused.entry_row_count == paused.entry_row_high_water == 0
    with pytest.raises(JournalCutoverPaused):
        JournalCaptureService(store, JournalContentAdapter(tmp_path / "vault")).accept(
            ingress=_ingress("paused"),
            client_mutation_id="capture-while-cutover-paused-0001",
            day_id=_day_id(),
            target=CaptureTarget.LOG,
            mode=CaptureMode.DUMB,
            exact_text="must not cross the cutover pause",
            input_mode="direct_entry",
            stated_at=None,
        )
    with pytest.raises(JournalAuthorityStateError, match="cutover gate is paused"):
        with legacy_markdown_write_guard(store.path):
            raise AssertionError("guard unexpectedly admitted the write")
    assert store.list_captures("2026-08-21") == []

    resumed = coordinator.resume_legacy_ingress(
        cohort_id=prepared.cohort_id,
        client_mutation_id="authority-resume-0001",
        actor={"kind": "migration_operator", "id": "test"},
    )
    assert resumed.cutover_gate_state == "open"
    assert coordinator.capture_mode() == "legacy_compatibility"


def test_legacy_markdown_guard_is_reentrant_and_serializes_authority_seal(
    tmp_path: Path,
) -> None:
    store = JournalCaptureStore(tmp_path / "journal.db")
    attempted = threading.Event()
    sealed = threading.Event()
    failures: list[BaseException] = []

    def seal() -> None:
        try:
            with sqlite3.connect(store.path, timeout=5.0) as conn:
                conn.execute("PRAGMA busy_timeout = 5000")
                attempted.set()
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE journal_authority_control "
                    "SET mode='database_only' WHERE singleton=1"
                )
                conn.commit()
            sealed.set()
        except BaseException as exc:  # pragma: no cover - diagnostic handoff
            failures.append(exc)

    thread = threading.Thread(target=seal, daemon=True)
    with legacy_markdown_write_guard(store.path):
        # A compatibility adapter can re-enter through ``vault_write`` without
        # trying to acquire its own SQLite writer lock.
        with legacy_markdown_write_guard(store.path):
            thread.start()
            assert attempted.wait(2.0)
            assert not sealed.wait(0.15)

    thread.join(5.0)
    assert not thread.is_alive()
    assert failures == []
    assert sealed.is_set()
    with pytest.raises(JournalAuthorityStateError, match="database_only"):
        with legacy_markdown_write_guard(store.path):
            raise AssertionError("sealed Markdown guard unexpectedly admitted")


def test_activation_rechecks_the_durable_pause_high_water(tmp_path: Path):
    store = JournalCaptureStore(tmp_path / "journal.db")
    cohort_id, _sources = _seal_empty_cohort(tmp_path, store)
    with store.transaction() as conn:
        conn.execute(
            "UPDATE journal_cutover_gate SET capture_row_count=1 WHERE singleton=1"
        )

    with pytest.raises(JournalAuthorityStateError, match="changed after"):
        JournalAuthorityCoordinator(store).activate_database_only(
            cohort_id=cohort_id,
            client_mutation_id="authority-high-water-activate-0001",
            actor={"kind": "migration_operator", "id": "test"},
        )


def test_postseal_capture_is_durably_queued_then_controlled_drain_releases(
    tmp_path: Path,
):
    store = JournalCaptureStore(tmp_path / "journal.db")
    cohort_id, sources = _seal_empty_cohort(tmp_path, store)
    coordinator = JournalAuthorityCoordinator(store)
    coordinator.activate_database_only(
        cohort_id=cohort_id,
        client_mutation_id="authority-queued-activate-0001",
        actor={"kind": "migration_operator", "id": "test"},
    )
    context = replace(_context(), permitted_purposes=("journal.materialize",))
    service = JournalCaptureService(store, JournalContentAdapter(tmp_path / "vault"))
    ingress = JournalCaptureIngress(
        sources,
        service,
        service_principal=context.service_principal,
        worker_id="journal-queued-test",
    )

    with pytest.raises(JournalIngressQueued) as queued:
        ingress.submit(
            trusted=context,
            exact_text="queued during postseal maintenance",
            client_mutation_id="authority-queued-capture-0001",
            day_id=_day_id(),
            target=CaptureTarget.RUNNING_NOTES,
            mode=CaptureMode.DUMB,
            input_mode="direct_entry",
        )
    assert queued.value.retryable is True
    effect_id = queued.value.commit.effect_id
    assert effect_id is not None
    effect = SourceOutbox(sources).get(effect_id)
    assert effect is not None and effect.status == "retryable"
    assert effect.error_code == "journal_cutover_ingress_paused"
    assert store.list_captures("2026-08-21") == []

    drain_id = "authority-queued-drain-0001"
    drained = JournalSourceDispatcher(
        sources,
        service,
        service_principal=context.service_principal,
        worker_id="journal-queued-drain-test",
    ).drain_postseal_held(
        cohort_id=cohort_id,
        client_mutation_id=drain_id,
        actor={"kind": "migration_operator", "id": "test"},
    )
    assert drained["status"] == "drained"
    assert SourceOutbox(sources).get(effect_id).status == "succeeded"
    assert len(store.list_captures("2026-08-21")) == 1
    assert coordinator.state().cutover_gate_state == "postseal_pending"

    coordinator.release_postseal_ingress(
        cohort_id=cohort_id,
        client_mutation_id="authority-queued-release-0001",
        actor={"kind": "migration_operator", "id": "test"},
        source_drain_mutation_id=drain_id,
        sources=sources,
        rehearsal_evidence_sha256s={
            "databaseCheckpoint": "a" * 64,
            "search": "b" * 64,
            "detachment": "c" * 64,
        },
        allow_unvalidated_rehearsal=True,
        rehearsal_authorization=_rehearsal(store),
    )


def test_controlled_drain_excludes_later_source_commands_until_a_new_batch(
    tmp_path: Path,
) -> None:
    store = JournalCaptureStore(tmp_path / "journal.db")
    cohort_id, sources = _seal_empty_cohort(tmp_path, store)
    coordinator = JournalAuthorityCoordinator(store)
    actor = {"kind": "migration_operator", "id": "test"}
    coordinator.activate_database_only(
        cohort_id=cohort_id,
        client_mutation_id="authority-bounded-activate-0001",
        actor=actor,
    )
    context = replace(_context(), permitted_purposes=("journal.materialize",))
    service = JournalCaptureService(store, JournalContentAdapter(tmp_path / "vault"))
    ingress = JournalCaptureIngress(
        sources,
        service,
        service_principal=context.service_principal,
        worker_id="journal-bounded-ingress",
    )

    def queue(mutation_id: str, text: str) -> str:
        with pytest.raises(JournalIngressQueued) as queued:
            ingress.submit(
                trusted=context,
                exact_text=text,
                client_mutation_id=mutation_id,
                day_id=_day_id(),
                target=CaptureTarget.RUNNING_NOTES,
                mode=CaptureMode.DUMB,
                input_mode="direct_entry",
            )
        assert queued.value.commit.effect_id is not None
        return str(queued.value.commit.effect_id)

    first_effect = queue("authority-bounded-capture-0001", "first bounded command")
    first_drain_id = "authority-bounded-drain-0001"
    bound = coordinator.bind_postseal_source_drain(
        sources=sources,
        cohort_id=cohort_id,
        client_mutation_id=first_drain_id,
        actor=actor,
    )
    assert bound["boundEffectIds"] == [first_effect]
    later_effect = queue("authority-bounded-capture-0002", "later bounded command")

    dispatcher = JournalSourceDispatcher(
        sources,
        service,
        service_principal=context.service_principal,
        worker_id="journal-bounded-drain",
    )
    first = dispatcher.drain_postseal_held(
        cohort_id=cohort_id,
        client_mutation_id=first_drain_id,
        actor=actor,
    )
    replay = dispatcher.drain_postseal_held(
        cohort_id=cohort_id,
        client_mutation_id=first_drain_id,
        actor=actor,
    )
    assert first["status"] == replay["status"] == "drained"
    assert SourceOutbox(sources).get(first_effect).status == "succeeded"
    assert SourceOutbox(sources).get(later_effect).status == "retryable"
    assert len(store.list_captures("2026-08-21")) == 1

    release = {
        "cohort_id": cohort_id,
        "client_mutation_id": "authority-bounded-release-0001",
        "actor": actor,
        "source_drain_mutation_id": first_drain_id,
        "sources": sources,
        "rehearsal_evidence_sha256s": {
            "databaseCheckpoint": "a" * 64,
            "search": "b" * 64,
            "detachment": "c" * 64,
        },
        "allow_unvalidated_rehearsal": True,
        "rehearsal_authorization": _rehearsal(store),
    }
    with pytest.raises(JournalAuthorityStateError, match="changed after"):
        coordinator.release_postseal_ingress(**release)

    second_drain_id = "authority-bounded-drain-0002"
    second = dispatcher.drain_postseal_held(
        cohort_id=cohort_id,
        client_mutation_id=second_drain_id,
        actor=actor,
    )
    assert second["status"] == "drained"
    assert SourceOutbox(sources).get(later_effect).status == "succeeded"
    assert len(store.list_captures("2026-08-21")) == 2
    coordinator.release_postseal_ingress(
        **{
            **release,
            "client_mutation_id": "authority-bounded-release-0002",
            "source_drain_mutation_id": second_drain_id,
        }
    )


def test_controlled_drain_defers_task_follow_up_until_release(tmp_path: Path) -> None:
    store = JournalCaptureStore(tmp_path / "journal.db")
    cohort_id, sources = _seal_empty_cohort(tmp_path, store)
    coordinator = JournalAuthorityCoordinator(store)
    actor = {"kind": "migration_operator", "id": "test"}
    coordinator.activate_database_only(
        cohort_id=cohort_id,
        client_mutation_id="authority-follow-up-activate-0001",
        actor=actor,
    )
    calls: list[str] = []

    class Proposals:
        def create_task_proposal(self, **_kwargs):
            calls.append("create")
            return {"ok": True, "proposal": {"thread_id": "th-0123abcd"}}

        def get(self, _thread_id):
            return {"ok": True, "proposal": {"status": "ready"}}

    context = replace(_context(), permitted_purposes=("journal.materialize",))
    service = JournalCaptureService(
        store,
        JournalContentAdapter(tmp_path / "vault"),
        proposal_service=Proposals(),
    )
    ingress = JournalCaptureIngress(
        sources,
        service,
        service_principal=context.service_principal,
        worker_id="journal-follow-up-ingress",
    )
    with pytest.raises(JournalIngressQueued):
        ingress.submit(
            trusted=context,
            exact_text="review this possible task",
            client_mutation_id="authority-follow-up-capture-0001",
            day_id=_day_id(),
            target=CaptureTarget.RUNNING_NOTES,
            mode=CaptureMode.DUMB,
            input_mode="direct_entry",
            follow_up_action="task_proposal",
        )
    drain_id = "authority-follow-up-drain-0001"
    drained = JournalSourceDispatcher(
        sources,
        service,
        service_principal=context.service_principal,
        worker_id="journal-follow-up-drain",
    ).drain_postseal_held(
        cohort_id=cohort_id,
        client_mutation_id=drain_id,
        actor=actor,
    )
    assert drained["status"] == "drained"
    assert calls == []
    with pytest.raises(JournalCutoverPaused):
        service.reconcile_proposals()
    assert calls == []

    coordinator.release_postseal_ingress(
        cohort_id=cohort_id,
        client_mutation_id="authority-follow-up-release-0001",
        actor=actor,
        source_drain_mutation_id=drain_id,
        sources=sources,
        rehearsal_evidence_sha256s={
            "databaseCheckpoint": "a" * 64,
            "search": "b" * 64,
            "detachment": "c" * 64,
        },
        allow_unvalidated_rehearsal=True,
        rehearsal_authorization=_rehearsal(store),
    )
    report = service.reconcile_proposals()
    assert report["delivery_checked"] == 1
    assert calls == ["create"]


def test_controlled_drain_defers_smart_model_until_explicit_postrelease_retry(
    tmp_path: Path,
) -> None:
    store = JournalCaptureStore(tmp_path / "journal.db")
    cohort_id, sources = _seal_empty_cohort(tmp_path, store)
    coordinator = JournalAuthorityCoordinator(store)
    actor = {"kind": "migration_operator", "id": "test"}
    coordinator.activate_database_only(
        cohort_id=cohort_id,
        client_mutation_id="authority-smart-activate-0001",
        actor=actor,
    )
    calls: list[str] = []

    class Processor:
        def process(self, **_kwargs):
            calls.append("model")
            return SmartCaptureResult(
                target=CaptureTarget.RUNNING_NOTES,
                summary="kept as a note",
                effects=(),
            )

    context = replace(
        _context(),
        permitted_purposes=("journal.smart_processing",),
    )
    service = JournalCaptureService(
        store,
        JournalContentAdapter(tmp_path / "vault"),
        smart_processor=Processor(),
    )
    ingress = JournalCaptureIngress(
        sources,
        service,
        service_principal=context.service_principal,
        worker_id="journal-smart-ingress",
    )
    with pytest.raises(JournalIngressQueued):
        ingress.submit(
            trusted=context,
            exact_text="contextualize this note",
            client_mutation_id="authority-smart-capture-0001",
            day_id=_day_id(),
            target=CaptureTarget.RUNNING_NOTES,
            mode=CaptureMode.SMART,
            input_mode="direct_entry",
            smart_disclosure_sha256=service.smart_disclosure_sha256,
        )
    drain_id = "authority-smart-drain-0001"
    drained = JournalSourceDispatcher(
        sources,
        service,
        service_principal=context.service_principal,
        worker_id="journal-smart-drain",
    ).drain_postseal_held(
        cohort_id=cohort_id,
        client_mutation_id=drain_id,
        actor=actor,
    )
    assert drained["status"] == "drained"
    capture = store.get_capture_by_mutation("authority-smart-capture-0001")
    assert capture is not None
    assert calls == []
    with pytest.raises(JournalCutoverPaused):
        service.process_smart(capture.capture_id, exact_text="contextualize this note")
    assert calls == []

    coordinator.release_postseal_ingress(
        cohort_id=cohort_id,
        client_mutation_id="authority-smart-release-0001",
        actor=actor,
        source_drain_mutation_id=drain_id,
        sources=sources,
        rehearsal_evidence_sha256s={
            "databaseCheckpoint": "a" * 64,
            "search": "b" * 64,
            "detachment": "c" * 64,
        },
        allow_unvalidated_rehearsal=True,
        rehearsal_authorization=_rehearsal(store),
    )
    service.process_smart(capture.capture_id, exact_text="contextualize this note")
    assert calls == ["model"]


def test_authority_activation_retains_fence_until_evidence_release(tmp_path: Path):
    store = JournalCaptureStore(tmp_path / "journal.db")
    cohort_id, sources = _seal_empty_cohort(tmp_path, store)
    coordinator = JournalAuthorityCoordinator(store)

    activated = coordinator.activate_database_only(
        cohort_id=cohort_id,
        client_mutation_id="authority-rehearsal-activate-0001",
        actor={"kind": "migration_operator", "id": "test"},
    )
    replay = coordinator.activate_database_only(
        cohort_id=cohort_id,
        client_mutation_id="authority-rehearsal-activate-0001",
        actor={"kind": "migration_operator", "id": "test"},
    )

    assert replay == activated
    assert activated.reversible_to_legacy is False
    assert activated.cutover_gate_state == "postseal_pending"
    assert JournalDomainService(store).authority_state() == "database_only"
    with pytest.raises(JournalCutoverPaused):
        coordinator.capture_mode()
    with pytest.raises(JournalAuthorityStateError, match="roll-forward only"):
        coordinator.rollback_to_legacy(
            client_mutation_id="authority-rehearsal-rollback-0001",
            actor={"kind": "migration_operator", "id": "test"},
        )
    with pytest.raises(JournalAuthorityStateError, match="Markdown writes are fenced"):
        with legacy_markdown_write_guard(store.path):
            raise AssertionError("postseal fence unexpectedly admitted Markdown")

    drain_id = "authority-rehearsal-drain-0001"
    drained = JournalSourceDispatcher(
        sources,
        JournalCaptureService(store, JournalContentAdapter(tmp_path / "vault")),
        service_principal=_context().service_principal,
        worker_id="journal-rehearsal-drain",
    ).drain_postseal_held(
        cohort_id=cohort_id,
        client_mutation_id=drain_id,
        actor={"kind": "migration_operator", "id": "test"},
    )
    assert drained["status"] == "drained"
    release_args = {
        "cohort_id": cohort_id,
        "client_mutation_id": "authority-rehearsal-release-0001",
        "actor": {"kind": "migration_operator", "id": "test"},
        "source_drain_mutation_id": drain_id,
        "sources": sources,
        "rehearsal_evidence_sha256s": {
            "databaseCheckpoint": "a" * 64,
            "search": "b" * 64,
            "detachment": "c" * 64,
        },
        "allow_unvalidated_rehearsal": True,
        "rehearsal_authorization": _rehearsal(store),
    }
    with pytest.raises(CutoverMaintenanceError, match="authorization is required"):
        coordinator.release_postseal_ingress(
            **{
                key: value
                for key, value in release_args.items()
                if key != "rehearsal_authorization"
            }
        )
    released = coordinator.release_postseal_ingress(**release_args)
    assert coordinator.release_postseal_ingress(**release_args) == released
    assert released.cutover_gate_state == "open"
    assert coordinator.capture_mode() == "database_only"
    with pytest.raises(JournalCaptureConflict):
        coordinator.release_postseal_ingress(
            **{
                **release_args,
                "actor": {"kind": "migration_operator", "id": "different"},
            }
        )
    with pytest.raises(JournalAuthorityStateError, match="compatibility authority"):
        coordinator.resume_legacy_ingress(
            cohort_id=cohort_id,
            client_mutation_id="authority-postseal-resume-0001",
            actor={"kind": "migration_operator", "id": "test"},
        )

    with store._connect() as conn:
        receipt = conn.execute(
            "SELECT * FROM journal_cutover_release_receipts WHERE mutation_id=?",
            ("authority-rehearsal-release-0001",),
        ).fetchone()
        assert receipt is not None
        assert receipt["domain"] == "journal"
        assert receipt["high_water_sha256"]
        assert receipt["result_sha256"]
        assert [
            row[0]
            for row in conn.execute(
                "SELECT transition_kind FROM journal_cutover_gate_transitions "
                "ORDER BY gate_revision"
            ).fetchall()
        ] == ["bootstrap", "pause", "activate", "release"]


def test_verified_import_does_not_implicitly_or_prematurely_flip_authority(tmp_path: Path):
    store = JournalCaptureStore(tmp_path / "journal.db")
    root = tmp_path / "frozen-history"
    root.mkdir()
    sources = SourceStore.create(tmp_path / "staged-sources")
    importer = LegacyJournalImportService(store, sources)
    prepared = importer.prepare(
        root,
        mapping=LegacyJournalImportMapping("empty-test/v1", {}),
        client_mutation_id="authority-unsealed-import-prepare-0001",
        actor={"kind": "migration_operator", "id": "test"},
    )
    importer.stage(prepared.cohort_id, root, ingress_context=_context())
    importer.verify(prepared.cohort_id, root)
    coordinator = JournalAuthorityCoordinator(store)
    coordinator.pause_legacy_ingress(
        cohort_id=prepared.cohort_id,
        client_mutation_id="authority-unsealed-pause-0001",
        actor={"kind": "migration_operator", "id": "test"},
    )

    assert coordinator.state().mode == "legacy_compatibility"
    with pytest.raises(JournalAuthorityStateError, match="verified and sealed"):
        coordinator.activate_database_only(
            cohort_id=prepared.cohort_id,
            client_mutation_id="authority-unsealed-activate-0001",
            actor={"kind": "migration_operator", "id": "test"},
        )
    assert coordinator.state().mode == "legacy_compatibility"

    importer.seal(prepared.cohort_id, root)
    activated = coordinator.activate_database_only(
        cohort_id=prepared.cohort_id,
        client_mutation_id="authority-sealed-activate-0001",
        actor={"kind": "migration_operator", "id": "test"},
    )
    assert activated.mode == "database_only"


def test_recovery_fence_blocks_capture_without_writing_and_restores_prior_mode(
    tmp_path: Path,
):
    store = JournalCaptureStore(tmp_path / "journal.db")
    coordinator = _activate(tmp_path, store)
    fenced = coordinator.fence_recovery(
        fence_code="isolated_rehearsal",
        client_mutation_id="authority-fence-0001",
        actor={"kind": "recovery_operator", "id": "test"},
    )
    service = JournalCaptureService(store, JournalContentAdapter(tmp_path / "vault"))

    assert fenced.mode == "recovery_fenced"
    assert fenced.prior_mode == "database_only"
    assert fenced.fence_code == "isolated_rehearsal"
    with pytest.raises(JournalAuthorityFenced):
        service.accept(
            ingress=_ingress("fenced"),
            client_mutation_id="capture-while-fenced-0001",
            day_id=_day_id(),
            target=CaptureTarget.LOG,
            mode=CaptureMode.DUMB,
            exact_text="must not be persisted while fenced",
            input_mode="direct_entry",
            stated_at=None,
        )
    assert store.list_captures("2026-08-21") == []

    recovered = coordinator.recover(
        client_mutation_id="authority-recover-0001",
        actor={"kind": "recovery_operator", "id": "test"},
    )
    assert recovered.mode == "database_only"
    assert recovered.fence_code is None
    assert recovered.prior_mode is None


def test_database_only_capture_is_atomic_searchable_and_never_projects_a_file(
    tmp_path: Path,
):
    store = JournalCaptureStore(tmp_path / "journal.db")
    coordinator = _activate(tmp_path, store)
    vault = tmp_path / "vault"
    journal_dir = vault / "journal"
    journal_dir.mkdir(parents=True)
    legacy_file = journal_dir / "2026-08-21.md"
    sentinel = b"legacy file must remain unchanged\r\n"
    legacy_file.write_bytes(sentinel)
    service = JournalCaptureService(store, JournalContentAdapter(vault))

    capture = service.accept(
        ingress=_ingress(),
        client_mutation_id="native-only-capture-0001",
        day_id=_day_id(),
        target=CaptureTarget.RUNNING_NOTES,
        mode=CaptureMode.DUMB,
        exact_text="exact database-only note  ",
        input_mode="direct_entry",
        stated_at="2026-08-21T09:00:00-04:00",
        submitted_at="2026-08-21T13:00:00+00:00",
    )

    item_id = coordinator.native_item_for_capture(capture.capture_id)
    assert capture.entry_id is None
    assert capture.resolved_target is CaptureTarget.RUNNING_NOTES
    assert item_id is not None
    assert legacy_file.read_bytes() == sentinel
    assert store.list_running_notes("2026-08-21") == []
    items = JournalDomainService(store).list_native_items("2026-08-21")
    assert len(items) == 1
    assert items[0].item_id == item_id
    assert items[0].plain_value == "exact database-only note  "
    assert len(JournalDomainService(store).pending_search_events()) == 1
    assert [ref.item_id for ref in JournalPartition(store).discover()] == [
        f"item:{item_id}"
    ]
    state = coordinator.state()
    assert state.first_native_capture_id == capture.capture_id
    assert state.first_native_item_id == item_id
    assert state.reversible_to_legacy is False
    with pytest.raises(JournalAuthorityStateError, match="roll-forward only"):
        coordinator.rollback_to_legacy(
            client_mutation_id="authority-too-late-rollback-0001",
            actor={"kind": "migration_operator", "id": "test"},
        )
    with store._connect() as conn:
        effect = conn.execute(
            "SELECT state,result_json FROM journal_effects "
            "WHERE capture_id=? AND effect_type='materialize'",
            (capture.capture_id,),
        ).fetchone()
        assert effect["state"] == "succeeded"
        assert item_id in effect["result_json"]
        assert conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 0

    replay = service.accept(
        ingress=_ingress(),
        client_mutation_id="native-only-capture-0001",
        day_id=_day_id(),
        target=CaptureTarget.RUNNING_NOTES,
        mode=CaptureMode.DUMB,
        exact_text="exact database-only note  ",
        input_mode="direct_entry",
        stated_at="2026-08-21T09:00:00-04:00",
        submitted_at="2026-08-21T13:00:00+00:00",
    )
    assert replay.capture_id == capture.capture_id
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM journal_items").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_native_capture_bindings"
        ).fetchone()[0] == 1


def test_native_source_redaction_scrubs_all_revision_prose_and_search_visibility(
    tmp_path: Path,
):
    store = JournalCaptureStore(tmp_path / "journal.db")
    coordinator = _activate(tmp_path, store)
    service = JournalCaptureService(store, JournalContentAdapter(tmp_path / "vault"))
    secret = "readable native secret that must be scrubbed"
    ingress = _ingress("redaction")
    capture = service.accept(
        ingress=ingress,
        client_mutation_id="native-redaction-capture-0001",
        day_id=_day_id(),
        target=CaptureTarget.LOG,
        mode=CaptureMode.DUMB,
        exact_text=secret,
        input_mode="direct_entry",
        stated_at=None,
    )
    item_id = coordinator.native_item_for_capture(capture.capture_id)
    assert item_id is not None

    result = store.mark_source_redacted(
        source_effect_id=ingress.effect_id,
        source_usage_id=ingress.usage_id or "usage-redaction",
        source_ref=ingress.source_ref,
        redaction_event_id="redaction-event-native-0001",
        redaction_epoch=1,
        result_sha256="b" * 64,
    )

    assert result is None
    assert JournalDomainService(store).list_native_items("2026-08-21") == ()
    assert list(JournalPartition(store).discover()) == []
    with store._connect() as conn:
        current = conn.execute(
            "SELECT current_plain_value,lifecycle,current_revision "
            "FROM journal_items WHERE item_id=?",
            (item_id,),
        ).fetchone()
        assert tuple(current) == ("[redacted]", "tombstoned", 2)
        revisions = conn.execute(
            "SELECT plain_value,lifecycle FROM journal_item_revisions "
            "WHERE item_id=? ORDER BY revision",
            (item_id,),
        ).fetchall()
        assert [tuple(row) for row in revisions] == [
            ("[redacted]", "tombstoned"),
            ("[redacted]", "tombstoned"),
        ]
        assert all(secret not in str(row) for row in revisions)
        native_receipt = conn.execute(
            "SELECT state,scrubbed_revision FROM journal_native_redactions "
            "WHERE redaction_event_id='redaction-event-native-0001'"
        ).fetchone()
        assert tuple(native_receipt) == ("committed", 2)
        source_receipt = conn.execute(
            "SELECT native_item_id FROM journal_source_redactions "
            "WHERE redaction_event_id='redaction-event-native-0001'"
        ).fetchone()
        assert source_receipt["native_item_id"] == item_id
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_search_outbox "
            "WHERE aggregate_id=? AND event_kind='delete'",
            (item_id,),
        ).fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="is_immutable"):
            conn.execute(
                "UPDATE journal_item_revisions SET plain_value='restored' "
                "WHERE item_id=? AND revision=1",
                (item_id,),
            )

    replay = store.mark_source_redacted(
        source_effect_id=ingress.effect_id,
        source_usage_id=ingress.usage_id or "usage-redaction",
        source_ref=ingress.source_ref,
        redaction_event_id="redaction-event-native-0001",
        redaction_epoch=1,
        result_sha256="b" * 64,
    )
    assert replay is None
