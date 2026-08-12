from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from work_buddy.task_notes import (
    AuthorityState,
    JournalAuthorityCoordinator,
    SagaState,
    TaskNoteContentAdapter,
    TaskNoteCutoverBlocked,
    TaskNoteMigrationStore,
)
from work_buddy.task_notes.operator import TaskNoteMigrationOperator
from tests.unit.task_notes.support import current_journal_exit_evidence


class _Bridge:
    def __init__(self) -> None:
        self.files: dict[str, str] = {}

    def read_file(self, path: str) -> str | None:
        return self.files.get(path)

    def write_file(self, path: str, content: str, **_kwargs) -> bool:
        self.files[path] = content
        return True

    def eval_js_internal(self, script: str) -> str:
        for path in tuple(self.files):
            if path in script:
                del self.files[path]
                return "deleted"
        return "not_found"


def _parity(store: TaskNoteMigrationStore, note_uuid: str) -> None:
    store.record_shadow(
        note_uuid=note_uuid,
        source_ref="wb-source://authority/item",
        source_content_sha256="a" * 64,
        legacy_file_sha256="a" * 64,
        legacy_normalized_sha256="a" * 64,
        document_projection_sha256="a" * 64,
        document_normalized_sha256="a" * 64,
        binding_id="b" * 32,
        store_id="c" * 32,
        document_id="d" * 32,
        byte_parity=True,
        normalized_parity=True,
        domain_revision="a" * 64,
    )


def test_cutover_is_per_note_and_requires_journal_exit_and_parity(tmp_path: Path) -> None:
    store = TaskNoteMigrationStore(tmp_path / "migration.db")
    _parity(store, "note-one")
    store.mark_shadow("tasks", "task_note", "note-two", domain_revision="r")

    with pytest.raises(TaskNoteCutoverBlocked):
        store.cutover(
            "tasks", "task_note", "note-one", domain_revision="cowork-1"
        )
    store.set_gate("task_note_cutover_gate", True)
    first = store.cutover(
        "tasks",
        "task_note",
        "note-one",
        domain_revision="cowork-1",
        rollback_deadline="2099-01-01T00:00:00+00:00",
        journal_exit_evidence=current_journal_exit_evidence(),
    )
    assert first.state is AuthorityState.COWORK
    assert first.epoch == 1
    with pytest.raises(TaskNoteCutoverBlocked):
        store.cutover(
            "tasks",
            "task_note",
            "note-two",
            domain_revision="cowork-2",
            rollback_deadline="2099-01-01T00:00:00+00:00",
            journal_exit_evidence=current_journal_exit_evidence(),
        )
    assert store.get_authority("tasks", "task_note", "note-two").state is (  # type: ignore[union-attr]
        AuthorityState.SHADOW
    )
    status = store.status_summary()
    assert "journal_exit_gate" not in status["gates"]  # type: ignore[operator]
    assert status["comparisons"] == {"parity": 1}  # type: ignore[comparison-overlap]


def test_operator_uses_current_journal_evidence_instead_of_a_task_local_gate(
    tmp_path: Path, monkeypatch
) -> None:
    migrations = SimpleNamespace(
        get_task_note=lambda _note_uuid: SimpleNamespace(
            source_content_sha256="a" * 64
        )
    )
    without_evidence = TaskNoteMigrationOperator(
        vault_root=tmp_path,
        migrations=migrations,  # type: ignore[arg-type]
        sources=object(),  # type: ignore[arg-type]
        principal=object(),  # type: ignore[arg-type]
        stores=object(),  # type: ignore[arg-type]
        journal_exit_evidence_provider=lambda: None,
    )
    with pytest.raises(TaskNoteCutoverBlocked, match="current Journal exit evidence"):
        without_evidence.cutover(
            "note-one", rollback_deadline="2099-01-01T00:00:00+00:00"
        )

    evidence = current_journal_exit_evidence()
    received: list[object] = []

    class Importer:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def cutover(self, _note_uuid, **kwargs):
            received.append(kwargs["journal_exit_evidence"])
            return (
                SimpleNamespace(
                    state=AuthorityState.COWORK,
                    epoch=1,
                    rollback_deadline=kwargs["rollback_deadline"],
                ),
                SimpleNamespace(binding_id="b" * 32),
            )

    with_evidence = TaskNoteMigrationOperator(
        vault_root=tmp_path,
        migrations=migrations,  # type: ignore[arg-type]
        sources=object(),  # type: ignore[arg-type]
        principal=object(),  # type: ignore[arg-type]
        stores=object(),  # type: ignore[arg-type]
        journal_exit_evidence_provider=lambda: evidence,
    )
    monkeypatch.setattr(with_evidence, "_importer", lambda: Importer())
    result = with_evidence.cutover(
        "note-one", rollback_deadline="2099-01-01T00:00:00+00:00"
    )
    assert received == [evidence]
    assert result["journalExitReceiptId"] == evidence["receipt_id"]


def test_journal_epochs_are_independent_and_never_touch_markdown(tmp_path: Path) -> None:
    store = TaskNoteMigrationStore(tmp_path / "migration.db")
    journal = JournalAuthorityCoordinator(store)
    note = journal.running_note("running-note-1", revision="legacy-1")
    log = journal.logical_day_log("2026-08-09", revision="legacy-log")
    assert note.epoch == log.epoch == 0
    journal.mark_shadow("running_note", "running-note-1", revision="shadow-1")
    store.set_gate("journal_cutover_gate", True)
    advanced = journal.cutover(
        "running_note", "running-note-1", revision="cowork-1"
    )
    assert advanced.state is AuthorityState.COWORK
    assert store.get_authority(
        "journal", "logical_day_log", "2026-08-09"
    ).state is AuthorityState.LEGACY  # type: ignore[union-attr]


def test_legacy_adapter_preserves_uuid_and_creation_saga_recovers(tmp_path: Path) -> None:
    bridge = _Bridge()
    migrations = TaskNoteMigrationStore(tmp_path / "migration.db")
    adapter = TaskNoteContentAdapter(
        vault_root=tmp_path / "vault",
        bridge_client=bridge,
        migration_store=migrations,
    )
    content = "# Task\n\nInitial body.\n"
    created = adapter.create(
        "stable-note-uuid",
        content,
        idempotency_key="create-request-1",
        task_id="t-11111111",
    )
    assert created.changed is True
    assert adapter.read("stable-note-uuid", filesystem_fallback=False) == content
    saga = migrations.get_saga(created.saga_id)  # type: ignore[arg-type]
    assert saga is not None and saga.state is SagaState.RUNNING
    assert saga.completed_steps == ("note",)

    # Retry after the note write but before master/store acknowledgement is
    # occurrence-safe and continues the same saga rather than minting a note.
    recovered = adapter.create(
        "stable-note-uuid",
        content,
        idempotency_key="create-request-1",
        task_id="t-11111111",
    )
    assert recovered.saga_id == created.saga_id
    assert recovered.changed is False
    adapter.mark_saga_step(recovered.saga_id, "master")
    adapter.mark_saga_step(recovered.saga_id, "metadata")
    assert migrations.get_saga(created.saga_id).state is SagaState.COMPLETED  # type: ignore[arg-type,union-attr]

    appended = adapter.append(
        "stable-note-uuid",
        "## More\n\nDetail",
        idempotency_key="append-1",
    )
    assert appended.changed is True
    assert "## More" in adapter.read("stable-note-uuid")


def test_saga_idempotency_rejects_same_key_with_different_payload(tmp_path: Path) -> None:
    store = TaskNoteMigrationStore(tmp_path / "migration.db")
    first = store.begin_saga(
        operation="create",
        idempotency_key="same",
        request_sha256=hashlib.sha256(b"first").hexdigest(),
        note_uuid="note-one",
        task_id="t-1",
        required_steps=("note", "master", "metadata"),
    )
    assert first.state is SagaState.PREPARED
    with pytest.raises(Exception):
        store.begin_saga(
            operation="create",
            idempotency_key="same",
            request_sha256=hashlib.sha256(b"second").hexdigest(),
            note_uuid="note-one",
            task_id="t-1",
            required_steps=("note", "master", "metadata"),
        )


def test_delete_retirement_saga_recovers_after_note_step(tmp_path: Path) -> None:
    bridge = _Bridge()
    migrations = TaskNoteMigrationStore(tmp_path / "migration.db")
    adapter = TaskNoteContentAdapter(
        vault_root=tmp_path / "vault",
        bridge_client=bridge,
        migration_store=migrations,
    )
    bridge.files[adapter.relative_path("stable-note-delete")] = "# Delete me\n"

    first = adapter.delete(
        "stable-note-delete",
        idempotency_key="delete-request-1",
        task_id="t-22222222",
    )
    saga = migrations.get_saga(first.saga_id)  # type: ignore[arg-type]
    assert saga is not None and saga.state is SagaState.RUNNING
    assert set(saga.completed_steps) == {"note", "binding"}
    assert migrations.get_authority(
        "tasks", "task_note", "stable-note-delete"
    ).state is AuthorityState.RETIRED  # type: ignore[union-attr]

    # Retry after note/binding retirement but before the task metadata step.
    recovered = adapter.delete(
        "stable-note-delete",
        idempotency_key="delete-request-1",
        task_id="t-22222222",
    )
    assert recovered.saga_id == first.saga_id
    adapter.mark_saga_step(recovered.saga_id, "master")
    adapter.mark_saga_step(recovered.saga_id, "metadata")
    assert migrations.get_saga(first.saga_id).state is SagaState.COMPLETED  # type: ignore[arg-type,union-attr]
