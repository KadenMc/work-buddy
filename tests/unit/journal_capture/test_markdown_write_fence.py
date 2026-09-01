from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from work_buddy.journal_capture.authority import (
    JournalAuthorityStateError,
    existing_authority_mode,
    require_legacy_markdown_write,
)
from work_buddy.vault_index.authority_exclusions import LegacyRootAuthorityError


def _authority_db(path: Path, mode: str) -> Path:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE journal_authority_control("
            "singleton INTEGER PRIMARY KEY, mode TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO journal_authority_control(singleton,mode) VALUES(1,?)",
            (mode,),
        )
    return path


def test_existing_authority_read_is_inert_and_missing_means_compatibility(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.db"

    assert existing_authority_mode(missing) == "legacy_compatibility"
    assert not missing.exists()


@pytest.mark.parametrize("mode", ["database_only", "recovery_fenced"])
def test_database_authority_fences_legacy_markdown_writes(
    tmp_path: Path,
    mode: str,
) -> None:
    database = _authority_db(tmp_path / "journal.db", mode)

    assert existing_authority_mode(database) == mode
    with pytest.raises(JournalAuthorityStateError, match="Markdown writes are fenced"):
        require_legacy_markdown_write(database)


def test_vault_writer_checks_seal_before_bridge_probe_or_file_read(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from work_buddy.journal_capture import authority
    from work_buddy.obsidian import bridge, vault_writer

    data_root = tmp_path / "data"
    (data_root / "db").mkdir(parents=True)
    database = _authority_db(
        data_root / "db" / "journal_capture.db",
        "database_only",
    )
    vault = tmp_path / "vault"
    journal_dir = vault / "journal"
    journal_dir.mkdir(parents=True)
    note = journal_dir / "2026-08-27.md"
    note.write_text("frozen", encoding="utf-8")

    monkeypatch.setattr(
        authority,
        "existing_authority_mode",
        lambda path=None: existing_authority_mode(database),
    )
    monkeypatch.setattr(
        vault_writer,
        "load_config",
        lambda: {
            "vault_root": str(vault),
            "paths": {"data_root": str(data_root)},
            "obsidian": {"journal_dir": "journal"},
        },
    )
    monkeypatch.setattr(
        bridge,
        "is_available",
        lambda: (_ for _ in ()).throw(AssertionError("bridge probed")),
    )

    with pytest.raises(LegacyRootAuthorityError, match="Markdown writes are fenced"):
        vault_writer.vault_write(
            "journal/2026-08-27.md",
            note,
            "replacement",
        )

    assert note.read_text(encoding="utf-8") == "frozen"


def test_stale_backlog_rewrite_is_rejected_before_path_access(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from work_buddy.consent import grant_consent
    from work_buddy.journal_capture import authority
    from work_buddy.journal_backlog.rewrite import rewrite_running_notes

    database = _authority_db(tmp_path / "journal.db", "database_only")
    monkeypatch.setattr(
        authority,
        "existing_authority_mode",
        lambda path=None: existing_authority_mode(database),
    )
    grant_consent("journal.rewrite_running_notes", mode="once")

    with pytest.raises(JournalAuthorityStateError, match="Markdown writes are fenced"):
        rewrite_running_notes(
            journal_path=tmp_path / "does-not-exist.md",
            original_text="old",
            threads=[],
            routing_record={"items": []},
            original_file_content="old",
        )
