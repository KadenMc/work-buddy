"""Tests for ``work_buddy.storage.migrations`` — the MigrationRunner framework.

Focus areas:

- ``_hash_callable`` rejects only the edits we care about (bytecode /
  literal / name changes) and ignores cosmetic ones (docstring rewrites,
  comment changes, whitespace).
- The auto-rehash path silently re-stamps legacy ``hash_format`` rows
  on first encounter, leaves them strict thereafter.
- ``MigrationHashMismatch`` still fires for genuine post-ship edits.
"""

from __future__ import annotations

import sqlite3
import textwrap
import types
from pathlib import Path

import pytest

from work_buddy.storage.migrations import (
    HASH_FORMAT_CURRENT,
    Migration,
    MigrationHashMismatch,
    MigrationRunner,
)


# ─── Helpers ────────────────────────────────────────────────────────


def _compile_fn(src: str) -> types.FunctionType:
    """Compile a single function from source and return it.

    Used to build pairs of "cosmetically different / behaviorally
    identical" migrations and "behaviorally different" migrations
    without paying the import-roundtrip tax.
    """
    ns: dict[str, object] = {}
    exec(textwrap.dedent(src), ns)
    fn = next(v for v in ns.values() if isinstance(v, types.FunctionType))
    return fn


def _make_runner(name: str, migrations: list[Migration]) -> MigrationRunner:
    return MigrationRunner(name, migrations=migrations)


# ─── _hash_callable: behaviour vs cosmetics ────────────────────────


class TestHashCallable:
    def test_identical_functions_hash_identically(self):
        fn_a = _compile_fn(
            """
            def m(conn):
                conn.execute("CREATE TABLE t (id INTEGER)")
            """
        )
        fn_b = _compile_fn(
            """
            def m(conn):
                conn.execute("CREATE TABLE t (id INTEGER)")
            """
        )
        assert MigrationRunner._hash_callable(fn_a) == MigrationRunner._hash_callable(fn_b)

    def test_docstring_rewrite_is_ignored(self):
        fn_a = _compile_fn(
            """
            def m(conn):
                '''Original wording for the docstring.'''
                conn.execute("CREATE TABLE t (id INTEGER)")
            """
        )
        fn_b = _compile_fn(
            """
            def m(conn):
                '''Completely rewritten docstring with different content.'''
                conn.execute("CREATE TABLE t (id INTEGER)")
            """
        )
        assert MigrationRunner._hash_callable(fn_a) == MigrationRunner._hash_callable(fn_b)

    def test_adding_docstring_is_ignored(self):
        fn_a = _compile_fn(
            """
            def m(conn):
                conn.execute("CREATE TABLE t (id INTEGER)")
            """
        )
        fn_b = _compile_fn(
            """
            def m(conn):
                '''Newly added docstring.'''
                conn.execute("CREATE TABLE t (id INTEGER)")
            """
        )
        assert MigrationRunner._hash_callable(fn_a) == MigrationRunner._hash_callable(fn_b)

    def test_comment_edit_is_ignored(self):
        fn_a = _compile_fn(
            """
            def m(conn):
                # Old comment.
                conn.execute("CREATE TABLE t (id INTEGER)")
            """
        )
        fn_b = _compile_fn(
            """
            def m(conn):
                # Totally different comment with new context.
                conn.execute("CREATE TABLE t (id INTEGER)")
            """
        )
        assert MigrationRunner._hash_callable(fn_a) == MigrationRunner._hash_callable(fn_b)

    def test_whitespace_reformat_is_ignored(self):
        fn_a = _compile_fn(
            """
            def m(conn):
                conn.execute("CREATE TABLE t (id INTEGER)")
            """
        )
        fn_b = _compile_fn(
            """
            def m(conn):


                conn.execute("CREATE TABLE t (id INTEGER)")
            """
        )
        assert MigrationRunner._hash_callable(fn_a) == MigrationRunner._hash_callable(fn_b)

    def test_ddl_change_is_detected(self):
        fn_a = _compile_fn(
            """
            def m(conn):
                conn.execute("CREATE TABLE t (id INTEGER)")
            """
        )
        fn_b = _compile_fn(
            """
            def m(conn):
                conn.execute("CREATE TABLE t (id INTEGER, name TEXT)")
            """
        )
        assert MigrationRunner._hash_callable(fn_a) != MigrationRunner._hash_callable(fn_b)

    def test_added_statement_is_detected(self):
        fn_a = _compile_fn(
            """
            def m(conn):
                conn.execute("CREATE TABLE t (id INTEGER)")
            """
        )
        fn_b = _compile_fn(
            """
            def m(conn):
                conn.execute("CREATE TABLE t (id INTEGER)")
                conn.execute("CREATE INDEX idx_t ON t(id)")
            """
        )
        assert MigrationRunner._hash_callable(fn_a) != MigrationRunner._hash_callable(fn_b)

    def test_global_rename_is_detected(self):
        # Different globally-referenced names (sqlite3 vs sqlite) trip
        # the audit even when bytecode shapes are otherwise similar.
        fn_a = _compile_fn(
            """
            def m(conn):
                import sqlite3
                conn.execute("SELECT 1")
            """
        )
        fn_b = _compile_fn(
            """
            def m(conn):
                import os
                conn.execute("SELECT 1")
            """
        )
        assert MigrationRunner._hash_callable(fn_a) != MigrationRunner._hash_callable(fn_b)

    def test_no_code_fallback_does_not_crash(self):
        # functools.partial-ish: no __code__. We expect the qualname
        # fallback, not an exception.
        class Callme:
            __qualname__ = "Callme.__call__"

            def __call__(self, conn):
                pass

        fn = Callme()
        h = MigrationRunner._hash_callable(fn)
        assert isinstance(h, str) and len(h) == 64


# ─── End-to-end: runner against a real sqlite DB ────────────────────


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


def _m_create_t(conn: sqlite3.Connection) -> None:
    """Initial schema."""
    conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY)")


def _m_add_col(conn: sqlite3.Connection) -> None:
    """Add a name column."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(t)")}
    if "name" not in cols:
        conn.execute("ALTER TABLE t ADD COLUMN name TEXT")


class TestRunnerEndToEnd:
    def test_fresh_run_applies_all_migrations(self, db_path: Path):
        runner = _make_runner("testdb", [
            Migration(1, "initial", _m_create_t),
            Migration(2, "add col", _m_add_col),
        ])
        conn = sqlite3.connect(str(db_path))
        runner.run(conn)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 2
        # Both rows present, both at the current hash format.
        rows = conn.execute(
            "SELECT version, hash_format FROM _migration_history ORDER BY version"
        ).fetchall()
        assert rows == [(1, HASH_FORMAT_CURRENT), (2, HASH_FORMAT_CURRENT)]
        conn.close()

    def test_reopen_with_unchanged_source_is_clean(self, db_path: Path):
        runner = _make_runner("testdb", [Migration(1, "initial", _m_create_t)])
        conn = sqlite3.connect(str(db_path))
        runner.run(conn)
        conn.close()
        # Second open: should be a no-op (hashes match).
        conn = sqlite3.connect(str(db_path))
        runner.run(conn)
        conn.close()

    def test_behavioral_edit_raises_hash_mismatch(self, db_path: Path):
        runner = _make_runner("testdb", [Migration(1, "initial", _m_create_t)])
        conn = sqlite3.connect(str(db_path))
        runner.run(conn)
        conn.close()

        def _m_create_t_edited(conn: sqlite3.Connection) -> None:
            """Initial schema."""
            # New side-effect that wasn't there before.
            conn.execute(
                "CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, extra TEXT)"
            )

        edited_runner = _make_runner("testdb", [
            Migration(1, "initial", _m_create_t_edited),
        ])
        conn = sqlite3.connect(str(db_path))
        with pytest.raises(MigrationHashMismatch):
            edited_runner.run(conn)
        conn.close()

    def test_cosmetic_edit_does_not_raise(self, db_path: Path):
        runner = _make_runner("testdb", [Migration(1, "initial", _m_create_t)])
        conn = sqlite3.connect(str(db_path))
        runner.run(conn)
        conn.close()

        def _m_create_t_doc_only(conn: sqlite3.Connection) -> None:
            """Initial schema. (Wording rewritten; behavior unchanged.)"""
            # Different comment phrasing here.
            conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY)")

        cosmetic_runner = _make_runner("testdb", [
            Migration(1, "initial", _m_create_t_doc_only),
        ])
        conn = sqlite3.connect(str(db_path))
        cosmetic_runner.run(conn)  # must NOT raise
        conn.close()


# ─── Auto-rehash for legacy stamps ──────────────────────────────────


class TestLegacyRehash:
    def test_null_hash_format_is_silently_restamped(self, db_path: Path):
        # Build a DB with the new framework, then mutate the audit row
        # to simulate a legacy stamp from the old source-text era.
        runner = _make_runner("testdb", [Migration(1, "initial", _m_create_t)])
        conn = sqlite3.connect(str(db_path))
        runner.run(conn)
        # Simulate legacy stamp: wrong hash + NULL format.
        conn.execute(
            "UPDATE _migration_history "
            "SET code_hash = 'deadbeef' || hex(randomblob(28)), "
            "    hash_format = NULL WHERE version = 1"
        )
        conn.commit()
        conn.close()

        # Re-open: must NOT raise, and must re-stamp the row.
        conn = sqlite3.connect(str(db_path))
        runner.run(conn)
        row = conn.execute(
            "SELECT code_hash, hash_format FROM _migration_history WHERE version = 1"
        ).fetchone()
        assert row[1] == HASH_FORMAT_CURRENT
        # Hash should now match the current callable's hash.
        assert row[0] == MigrationRunner._hash_callable(_m_create_t)
        conn.close()

    def test_unknown_hash_format_is_also_restamped(self, db_path: Path):
        # Forward-compat: any hash_format != HASH_FORMAT_CURRENT is
        # treated as legacy and re-stamped.
        runner = _make_runner("testdb", [Migration(1, "initial", _m_create_t)])
        conn = sqlite3.connect(str(db_path))
        runner.run(conn)
        conn.execute(
            "UPDATE _migration_history "
            "SET code_hash = 'doesnotmatter', "
            "    hash_format = 'imaginary_old_format' WHERE version = 1"
        )
        conn.commit()
        conn.close()

        conn = sqlite3.connect(str(db_path))
        runner.run(conn)
        row = conn.execute(
            "SELECT hash_format FROM _migration_history WHERE version = 1"
        ).fetchone()
        assert row[0] == HASH_FORMAT_CURRENT
        conn.close()

    def test_strict_audit_after_restamp(self, db_path: Path):
        # After a legacy stamp gets re-stamped, subsequent behavioral
        # edits should still trip the audit.
        runner = _make_runner("testdb", [Migration(1, "initial", _m_create_t)])
        conn = sqlite3.connect(str(db_path))
        runner.run(conn)
        conn.execute(
            "UPDATE _migration_history "
            "SET code_hash = 'legacy', hash_format = NULL WHERE version = 1"
        )
        conn.commit()
        conn.close()

        # Pass 1: re-stamp.
        conn = sqlite3.connect(str(db_path))
        runner.run(conn)
        conn.close()

        # Pass 2: behavioral edit must raise.
        def _m_create_t_edited(conn: sqlite3.Connection) -> None:
            """Initial schema."""
            conn.execute(
                "CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, extra TEXT)"
            )

        edited_runner = _make_runner("testdb", [
            Migration(1, "initial", _m_create_t_edited),
        ])
        conn = sqlite3.connect(str(db_path))
        with pytest.raises(MigrationHashMismatch):
            edited_runner.run(conn)
        conn.close()


# ─── _ensure_history_schema: legacy column-add ──────────────────────


class TestEnsureHistorySchema:
    def test_legacy_history_table_gets_hash_format_column(self, db_path: Path):
        # Simulate a DB created by an older framework version: history
        # table exists, but without the hash_format column.
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE _migration_history ("
            "version INTEGER PRIMARY KEY, description TEXT NOT NULL, "
            "applied_at TEXT NOT NULL, code_hash TEXT NOT NULL)"
        )
        conn.execute("PRAGMA user_version = 1")
        conn.execute(
            "INSERT INTO _migration_history (version, description, applied_at, code_hash) "
            "VALUES (1, 'initial', '2026-01-01T00:00:00Z', 'oldhash')"
        )
        conn.commit()
        # Also create the table the migration would create, so the
        # baseline-stamp path doesn't kick in.
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        runner = _make_runner("testdb", [Migration(1, "initial", _m_create_t)])
        conn = sqlite3.connect(str(db_path))
        runner.run(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(_migration_history)")}
        assert "hash_format" in cols
        # The legacy row was treated as a legacy stamp and re-hashed.
        row = conn.execute(
            "SELECT code_hash, hash_format FROM _migration_history WHERE version = 1"
        ).fetchone()
        assert row[1] == HASH_FORMAT_CURRENT
        assert row[0] == MigrationRunner._hash_callable(_m_create_t)
        conn.close()
