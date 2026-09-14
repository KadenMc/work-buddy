"""Canary for pytest's process-level native-state isolation boundary."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid
from pathlib import Path

import pytest


def _row_exists(db_path: Path, sql: str, params: tuple[str, ...]) -> bool:
    if not db_path.is_file():
        return False
    connection = sqlite3.connect(
        db_path.resolve().as_uri() + "?mode=ro",
        uri=True,
        timeout=10,
    )
    try:
        return connection.execute(sql, params).fetchone() is not None
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return False
        raise
    finally:
        connection.close()


def _overlaps(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.is_dir():
        return digest.hexdigest()
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _sqlite_durable_state(db_path: Path) -> dict[str, tuple[int, str]]:
    """Hash SQLite's durable files without creating or checkpointing them."""
    state: dict[str, tuple[int, str]] = {}
    for suffix in ("", "-wal", "-journal"):
        path = Path(f"{db_path}{suffix}")
        if not path.is_file():
            continue
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        state[suffix] = (path.stat().st_size, digest.hexdigest())
    return state


def test_readonly_marker_check_includes_committed_wal_rows(tmp_path):
    """A live writer's committed WAL content must not be mistaken for absence."""
    db_path = tmp_path / "wal-visible.db"
    writer = sqlite3.connect(db_path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE markers (value TEXT NOT NULL)")
        writer.execute("INSERT INTO markers VALUES ('committed')")
        writer.commit()
        assert Path(f"{db_path}-wal").stat().st_size > 0
        assert _row_exists(
            db_path,
            "SELECT 1 FROM markers WHERE value=?",
            ("committed",),
        )
    finally:
        writer.close()


@pytest.mark.real_task_store
def test_task_session_and_knowledge_writes_stay_out_of_native_state(
    monkeypatch, native_state_roots
):
    """Exercise all three write roots and prove their markers remain isolated."""
    from work_buddy import agent_session, paths
    from work_buddy.knowledge import store as knowledge_store_module
    from work_buddy.knowledge.personal.store import PersonalKnowledgeStore
    from work_buddy.obsidian.tasks import store as task_store
    from work_buddy.tasks.store import TaskStore

    isolated_root = Path(os.environ["WORK_BUDDY_DATA_DIR"]).resolve()
    native_root = native_state_roots["data"]
    native_task_db = native_state_roots["task_db"]
    native_knowledge_db = native_root / "db" / "personal_knowledge.db"
    local_knowledge_root = native_state_roots["local_knowledge"]

    assert not _overlaps(isolated_root, native_root)
    assert paths.resolve("db/tasks").is_relative_to(isolated_root)
    assert paths.resolve("db/personal-knowledge").is_relative_to(isolated_root)
    assert paths.data_dir("agents").is_relative_to(isolated_root)
    assert knowledge_store_module._STORE_DIR.resolve() == native_state_roots["system_knowledge"]
    assert knowledge_store_module._LOCAL_DIR.resolve() == local_knowledge_root

    token = uuid.uuid4().hex
    task_id = f"t-pytest-isolation-{token}"
    session_id = f"{token}-pytest-isolation"
    table_name = f"pytest_isolation_{token}"
    local_knowledge_before = _tree_digest(local_knowledge_root)

    assert not _row_exists(
        native_task_db,
        "SELECT 1 FROM task_metadata WHERE task_id=?",
        (task_id,),
    )
    assert not _row_exists(
        native_knowledge_db,
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    )
    native_task_before = _sqlite_durable_state(native_task_db)
    native_knowledge_before = _sqlite_durable_state(native_knowledge_db)

    # Fail closed before the first write. This catches a configured absolute
    # tasks.db_path, which has precedence over WORK_BUDDY_DATA_DIR in both
    # native and legacy stores.
    native_task_store = TaskStore()
    assert native_task_store.path.resolve().is_relative_to(isolated_root)
    assert task_store._db_path().resolve().is_relative_to(isolated_root)
    native_task_store.initialize()
    task_store.create(task_id, description="pytest native-state isolation canary")

    monkeypatch.setattr(agent_session, "_cached_session_dir", None)
    session_dir = agent_session.get_session_dir(session_id)
    assert session_dir.resolve().is_relative_to(isolated_root)
    assert (session_dir / "manifest.json").is_file()

    knowledge_store = PersonalKnowledgeStore()
    assert knowledge_store.db_path.resolve().is_relative_to(isolated_root)
    connection = knowledge_store.connect()
    try:
        connection.execute(f'CREATE TABLE "{table_name}" (marker TEXT NOT NULL)')
        connection.execute(f'INSERT INTO "{table_name}" VALUES (?)', (token,))
        connection.commit()
    finally:
        connection.close()

    # System knowledge and store.local are asset-root inputs, not data-root
    # outputs. Loading them is allowed; their user-owned overlay must remain
    # byte-for-byte unchanged.
    knowledge_store_module.load_store(force=True, scope="system")
    assert _tree_digest(local_knowledge_root) == local_knowledge_before

    native_session_matches = list((native_root / "agents").glob(f"*_{session_id[:8]}"))
    assert native_session_matches == []
    assert not _row_exists(
        native_task_db,
        "SELECT 1 FROM task_metadata WHERE task_id=?",
        (task_id,),
    )
    assert not _row_exists(
        native_knowledge_db,
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    )
    assert _sqlite_durable_state(native_task_db) == native_task_before
    assert _sqlite_durable_state(native_knowledge_db) == native_knowledge_before
