from __future__ import annotations

from pathlib import Path
import sqlite3
import threading

import pytest

from work_buddy.vault_index.authority_exclusions import (
    LegacyRootAuthorityError,
    configured_legacy_roots,
    legacy_root_read_guard,
    legacy_root_write_guard,
)


def _config(tmp_path: Path) -> dict:
    vault = tmp_path / "vault"
    data = tmp_path / "data"
    vault.mkdir()
    (data / "db").mkdir(parents=True)
    return {
        "vault_root": str(vault),
        "paths": {"data_root": str(data)},
        "obsidian": {"journal_dir": "journal"},
        "projects": {"markdown_dir": "projects"},
        "contracts": {"vault_path": "contracts"},
        "personal_knowledge": {"vault_path": "personal"},
    }


def _authority_db(cfg: dict, domain: str, *, sealed: bool) -> Path:
    db_dir = Path(cfg["paths"]["data_root"]) / "db"
    declarations = {
        "journal": (
            db_dir / "journal_capture.db",
            "CREATE TABLE journal_authority_control("
            "singleton INTEGER PRIMARY KEY,mode TEXT NOT NULL)",
            "INSERT INTO journal_authority_control VALUES(1,?)",
            "database_only" if sealed else "legacy_compatibility",
        ),
        "projects": (
            db_dir / "projects.db",
            "CREATE TABLE project_authority_state("
            "singleton INTEGER PRIMARY KEY,authority TEXT NOT NULL,state TEXT NOT NULL)",
            "INSERT INTO project_authority_state VALUES(1,?, 'active')",
            "sqlite" if sealed else "legacy_markdown",
        ),
        "contracts": (
            db_dir / "contracts.db",
            "CREATE TABLE contract_authority("
            "singleton INTEGER PRIMARY KEY,state TEXT NOT NULL)",
            "INSERT INTO contract_authority VALUES(1,?)",
            "native" if sealed else "legacy",
        ),
        "personal_knowledge": (
            db_dir / "personal_knowledge.db",
            "CREATE TABLE personal_knowledge_authority("
            "singleton INTEGER PRIMARY KEY,authority TEXT NOT NULL)",
            "INSERT INTO personal_knowledge_authority VALUES(1,?)",
            "sqlite" if sealed else "legacy_markdown",
        ),
    }
    path, create_sql, insert_sql, value = declarations[domain]
    with sqlite3.connect(path) as conn:
        conn.execute(create_sql)
        conn.execute(insert_sql, (value,))
    return path


@pytest.mark.parametrize(
    "domain",
    ["journal", "projects", "contracts", "personal_knowledge"],
)
def test_all_sealed_configured_roots_reject_before_caller_archive_access(
    tmp_path: Path,
    domain: str,
) -> None:
    cfg = _config(tmp_path)
    root = configured_legacy_roots(cfg)[domain]
    root.mkdir(parents=True)
    target = root / "archived.md"
    target.write_text("frozen", encoding="utf-8")
    _authority_db(cfg, domain, sealed=True)
    touched = False

    with pytest.raises(LegacyRootAuthorityError, match="fenced|retired"):
        with legacy_root_write_guard(target, cfg=cfg):
            touched = True
            target.write_text("changed", encoding="utf-8")

    assert touched is False
    assert target.read_text(encoding="utf-8") == "frozen"


def test_legacy_root_guard_holds_domain_lock_through_file_operation(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    root = configured_legacy_roots(cfg)["projects"]
    root.mkdir(parents=True)
    target = root / "project.md"
    target.write_text("legacy", encoding="utf-8")
    database = _authority_db(cfg, "projects", sealed=False)
    seal_attempted = threading.Event()
    seal_finished = threading.Event()
    failures: list[BaseException] = []

    def seal() -> None:
        try:
            with sqlite3.connect(database, timeout=5.0) as conn:
                conn.execute("PRAGMA busy_timeout=5000")
                seal_attempted.set()
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE project_authority_state SET authority='sqlite' "
                    "WHERE singleton=1"
                )
                conn.commit()
            seal_finished.set()
        except BaseException as exc:  # pragma: no cover - diagnostic handoff
            failures.append(exc)

    thread = threading.Thread(target=seal, daemon=True)
    with legacy_root_write_guard(target, cfg=cfg):
        thread.start()
        assert seal_attempted.wait(2.0)
        assert not seal_finished.wait(0.15)
        target.write_text("last legacy write", encoding="utf-8")

    thread.join(5.0)
    assert not thread.is_alive()
    assert failures == []
    assert seal_finished.is_set()
    with pytest.raises(LegacyRootAuthorityError, match="sqlite"):
        with legacy_root_write_guard(target, cfg=cfg):
            raise AssertionError("sealed archive guard unexpectedly admitted")


def test_legacy_root_read_guard_serializes_parse_with_authority_seal(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    root = configured_legacy_roots(cfg)["projects"]
    root.mkdir(parents=True)
    target = root / "project.md"
    target.write_text("legacy", encoding="utf-8")
    database = _authority_db(cfg, "projects", sealed=False)
    seal_attempted = threading.Event()
    seal_finished = threading.Event()

    def seal() -> None:
        with sqlite3.connect(database, timeout=5.0) as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            seal_attempted.set()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE project_authority_state SET authority='sqlite' "
                "WHERE singleton=1"
            )
            conn.commit()
        seal_finished.set()

    thread = threading.Thread(target=seal, daemon=True)
    with legacy_root_read_guard(target, cfg=cfg):
        thread.start()
        assert seal_attempted.wait(2.0)
        assert not seal_finished.wait(0.15)
        assert target.read_text(encoding="utf-8") == "legacy"

    thread.join(5.0)
    assert not thread.is_alive()
    assert seal_finished.is_set()
    with pytest.raises(LegacyRootAuthorityError, match="sqlite"):
        with legacy_root_read_guard(target, cfg=cfg):
            target.read_text(encoding="utf-8")


def test_shared_vault_writer_fences_warm_direct_call_before_bridge_probe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from work_buddy.obsidian import bridge, vault_writer

    cfg = _config(tmp_path)
    root = configured_legacy_roots(cfg)["contracts"]
    root.mkdir(parents=True)
    target = root / "contract.md"
    target.write_text("frozen", encoding="utf-8")
    _authority_db(cfg, "contracts", sealed=True)
    monkeypatch.setattr(vault_writer, "load_config", lambda: cfg)
    monkeypatch.setattr(
        bridge,
        "is_available",
        lambda: (_ for _ in ()).throw(AssertionError("bridge probed")),
    )

    with pytest.raises(LegacyRootAuthorityError, match="contracts"):
        vault_writer.vault_write("contracts/contract.md", target, "changed")

    assert target.read_text(encoding="utf-8") == "frozen"


def test_location_resolver_is_fenced_before_scanning_sealed_journal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from work_buddy.obsidian import vault_writer

    cfg = _config(tmp_path)
    journal = configured_legacy_roots(cfg)["journal"]
    journal.mkdir(parents=True)
    _authority_db(cfg, "journal", sealed=True)
    monkeypatch.setattr(vault_writer, "load_config", lambda: cfg)
    monkeypatch.setattr(
        vault_writer,
        "_resolve_note_path",
        lambda *_args: (_ for _ in ()).throw(AssertionError("archive scanned")),
    )

    with pytest.raises(LegacyRootAuthorityError, match="Journal|journal"):
        vault_writer.write_at_location("new text", note="latest_journal")


def test_direct_append_cannot_bypass_sealed_non_journal_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from work_buddy.journal_backlog.route import _append_to_note_impl

    cfg = _config(tmp_path)
    projects = configured_legacy_roots(cfg)["projects"]
    projects.mkdir(parents=True)
    target = projects / "project.md"
    target.write_text("frozen", encoding="utf-8")
    _authority_db(cfg, "projects", sealed=True)
    monkeypatch.setattr("work_buddy.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        "work_buddy.health.preferences.is_wanted",
        lambda _component_id: None,
    )

    with pytest.raises(LegacyRootAuthorityError, match="projects"):
        _append_to_note_impl("changed", Path(cfg["vault_root"]), "projects/project.md")

    assert target.read_text(encoding="utf-8") == "frozen"
