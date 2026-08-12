from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from work_buddy.backups.source_foundation_restore import (
    SourceFoundationRestorePending,
    write_restore_fence,
)
from work_buddy.journal_capture.migration import JournalMigrationService
from work_buddy.journal_capture.models import JournalCaptureError
from work_buddy.journal_capture.dispatch import JournalSourceDispatcher
from work_buddy.journal_capture.store import JournalCaptureStore


@pytest.fixture
def isolated_fence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    marker = tmp_path / "restore" / "source_foundation_restore_pending.json"
    monkeypatch.setattr(
        "work_buddy.backups.source_foundation_restore.restore_fence_path",
        lambda: marker,
    )
    return marker


def test_journal_store_reopens_read_only_and_blocks_every_transaction(
    isolated_fence: Path,
    tmp_path: Path,
) -> None:
    path = tmp_path / "journal.db"
    JournalCaptureStore(path)
    write_restore_fence({"snapshot_id": "journal-fence"}, path=isolated_fence)

    reopened = JournalCaptureStore(path)
    assert reopened.list_migrations() == ()
    with pytest.raises(SourceFoundationRestorePending) as raised:
        with reopened.transaction():
            pass
    assert raised.value.operation == "journal_capture.write"


def test_journal_store_does_not_create_or_migrate_while_fenced(
    isolated_fence: Path,
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing" / "journal.db"
    write_restore_fence({"snapshot_id": "journal-missing"}, path=isolated_fence)
    with pytest.raises(JournalCaptureError, match="state_missing"):
        JournalCaptureStore(missing)
    assert not missing.exists()
    assert not missing.parent.exists()


def test_journal_store_rejects_stale_schema_without_upgrading_it(
    isolated_fence: Path,
    tmp_path: Path,
) -> None:
    path = tmp_path / "journal.db"
    JournalCaptureStore(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE journal_meta SET value='5' WHERE key='schema_version'"
        )
    before = path.read_bytes()
    write_restore_fence({"snapshot_id": "journal-stale"}, path=isolated_fence)

    with pytest.raises(JournalCaptureError, match="state_invalid"):
        JournalCaptureStore(path)
    assert path.read_bytes() == before
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT value FROM journal_meta WHERE key='schema_version'"
        ).fetchone() == ("5",)


def test_reconcile_fence_precedes_document_kernel_dispatch(
    isolated_fence: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reached_state = False
    service = object.__new__(JournalMigrationService)

    def forbidden_state(*_args, **_kwargs):
        nonlocal reached_state
        reached_state = True
        raise AssertionError("migration state must not be read before the fence")

    service._require_record = forbidden_state  # type: ignore[method-assign]
    write_restore_fence({"snapshot_id": "journal-dispatch"}, path=isolated_fence)
    with pytest.raises(SourceFoundationRestorePending) as raised:
        service.reconcile("logical_day_log", "2026-08-10")
    assert raised.value.operation == "journal_content_migration.reconcile"
    assert reached_state is False


@pytest.mark.parametrize("method,args", [("drain", ()), ("deliver_exact", ("effect",))])
def test_capture_dispatch_fence_precedes_source_lease_or_resolution(
    isolated_fence: Path,
    method: str,
    args: tuple[object, ...],
) -> None:
    class ForbiddenOutbox:
        def __getattr__(self, name: str):
            raise AssertionError(f"Sources outbox reached while fenced: {name}")

    dispatcher = object.__new__(JournalSourceDispatcher)
    dispatcher.outbox = ForbiddenOutbox()  # type: ignore[assignment]
    write_restore_fence({"snapshot_id": "journal-dispatch"}, path=isolated_fence)

    with pytest.raises(SourceFoundationRestorePending) as raised:
        getattr(dispatcher, method)(*args)
    assert raised.value.operation == "journal_capture.dispatch"


def test_cached_api_runtime_skips_startup_recovery_while_fenced(
    isolated_fence: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from work_buddy.journal_capture import api

    runtime = (object(), object(), object())
    monkeypatch.setattr(api, "_runtime", runtime)
    monkeypatch.setattr(api, "_recovery_complete", False)
    monkeypatch.setattr(
        api,
        "reconcile_journal_documents",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("document reconciliation ran while fenced")
        ),
    )
    write_restore_fence({"snapshot_id": "journal-runtime"}, path=isolated_fence)

    assert api._services() is runtime
    assert api._recovery_complete is False
