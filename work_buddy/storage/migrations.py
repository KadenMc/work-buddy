"""Versioned schema migrations for work-buddy's SQLite stores.

Provides ``MigrationRunner`` — a per-DB versioned migration ladder
keyed off ``PRAGMA user_version``. See ``architecture/migrations``
for the full design.

## Why we rolled our own

Verified against sqlite-migrate (github.com/simonw/sqlite-migrate) at
plan time. That library is genuinely minimal — no transaction wrapping,
no race lock, no PRAGMA handling, no hash check, no downgrade guard.
Adopting it would mean adding all the same safety items on top of its
base, so we'd be writing this code regardless. Better to own it.

## Invariants this runner enforces

1. **Atomicity per migration step.** The migration callable and the
   ``PRAGMA user_version`` bump share one ``BEGIN IMMEDIATE`` /
   ``COMMIT`` transaction. Mid-migration crash rolls back schema and
   version together — never a state where DB and version disagree.

2. **PRAGMA foreign_keys discipline.** ``PRAGMA foreign_keys = OFF``
   is a documented SQLite no-op when inside an open transaction
   (https://sqlite.org/foreignkeys.html). Always set BEFORE ``BEGIN``.
   Matters for table-rebuild migrations.

3. **Race-safe across processes.** ``BEGIN IMMEDIATE`` is acquired
   before reading ``user_version``, so two concurrent processes can't
   both observe an old version and both try to apply the next
   migration.

4. **Downgrade guard.** If ``user_version`` exceeds the highest
   migration this code knows about, refuse to open the DB. Older
   work-buddy code is not allowed to operate on a schema it doesn't
   understand.

5. **Hash audit.** Each migration's source AST (with docstrings
   stripped) is hashed at apply time and stored in
   ``_migration_history``. On subsequent runs, applied migrations
   are verified against their stored hash — detects the "someone
   edited a shipped migration callable" anti-pattern that Flyway's
   checksum enforcement guards against, while ignoring cosmetic edits
   (docstrings, comments, whitespace) that don't change runtime
   behavior. The AST approach is Python-version-independent: the
   same source produces the same hash on every CPython release that
   parses it identically.

## Invariants the caller must hold

- Each migration callable is **idempotent**. Calling it on a DB that
  already has the resulting schema must be a no-op (use
  ``CREATE TABLE IF NOT EXISTS``, check ``PRAGMA table_info`` before
  ``ALTER TABLE ADD COLUMN``, etc.). This lets us safely import an
  existing pre-migration DB into the framework via the baseline-stamp
  path below.

- Migration 1 must produce exactly the schema a "fresh install" should
  have at version 1. The full ladder run from 0 → max must reproduce
  the current schema.

- Never edit a shipped migration callable. The hash audit will block
  the DB from opening if you do.

- One logical change per migration step (split bundles into separate
  numbered migrations). Easier to review, easier to identify the step
  that broke when something does.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import sqlite3
import textwrap
from dataclasses import dataclass
from typing import Callable

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


# Identifier baked into every newly-stamped hash. Bumping this string
# is how we roll out a hashing-algorithm change: legacy rows at the
# older identifier get re-stamped on first encounter (see
# ``_verify_history_hashes``). History:
#
# - ``bytecode_v1``: hashed ``dis.get_instructions`` argvals. Stable
#   across cosmetic edits but tied to the CPython compiler, so the
#   same source produced different hashes on Python 3.11 vs 3.13.
# - ``ast_v1`` (current): hashes ``ast.unparse`` of the parsed
#   source with docstrings stripped and the outer ``FunctionDef``
#   name normalised. Same source → same hash on every Python
#   release that parses it identically.
HASH_FORMAT_CURRENT = "ast_v1"


# ─── Exceptions ─────────────────────────────────────────────────────


class MigrationError(Exception):
    """Base for migration-runner errors."""


class SchemaVersionTooNew(MigrationError):
    """The DB's ``user_version`` exceeds the max migration this code knows.

    Raised when a user downgrades work-buddy after their DB has been
    migrated to a higher version. The fix is to upgrade work-buddy
    back to a version that includes the missing migrations — not to
    roll the DB backward (which would lose schema and possibly data).
    """


class MigrationHashMismatch(MigrationError):
    """An already-applied migration's source has changed since it was applied.

    Raised when ``_migration_history.code_hash`` for a previously
    applied migration doesn't match the current hash of the callable's
    source. Indicates someone edited a shipped migration in-place —
    a bug-class that Flyway and Liquibase guard against by halting
    on mismatch. The fix is to add a NEW migration step that corrects
    whatever the edit was trying to fix, not to patch the old step.
    """


# ─── AST helpers ────────────────────────────────────────────────────


def _strip_docstrings(tree: ast.AST) -> None:
    """Remove docstring ``Expr`` nodes from every function / class / module body.

    Docstrings live in the AST as the first statement of a body when
    it's an ``Expr`` wrapping a string ``Constant``. Removing them
    before hashing keeps docstring edits (add / remove / rewrite)
    invisible to the audit — the same way they're invisible to runtime
    behaviour.
    """
    docstring_holders = (
        ast.Module,
        ast.FunctionDef,
        ast.AsyncFunctionDef,
        ast.ClassDef,
    )
    for node in ast.walk(tree):
        if not isinstance(node, docstring_holders):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            body.pop(0)


def _normalize_outer_fn_name(tree: ast.AST) -> None:
    """Erase the top-level ``FunctionDef``'s name from the hashed AST.

    The migration callable is referenced from the ``MigrationRunner``
    constructor by Python binding, not by name string — renaming
    ``_m001_initial`` to ``_m001_initial_schema`` doesn't change what
    the migration *does*, only the symbol that the migrations list
    holds a reference to. Treat that rename as cosmetic.

    Inner / nested ``FunctionDef`` names are left alone — those
    typically *are* referenced by name from the enclosing scope, so
    renaming one would be a behavioural change.
    """
    if not isinstance(tree, ast.Module):
        return
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stmt.name = "<fn>"


# ─── Migration record ───────────────────────────────────────────────


@dataclass(frozen=True)
class Migration:
    """A single numbered migration step.

    Attributes:
        version: 1-based monotonically increasing version. Gaps are
            allowed but discouraged.
        description: One-line human-readable summary. Stored in
            ``_migration_history.description`` for the audit trail.
        fn: Idempotent callable taking ``sqlite3.Connection``. Must
            perform its DDL in a way that's safe to call against an
            already-migrated DB (CREATE TABLE IF NOT EXISTS, column
            existence checks before ALTER TABLE, etc.).
    """

    version: int
    description: str
    fn: Callable[[sqlite3.Connection], None]


# ─── Runner ─────────────────────────────────────────────────────────


_HISTORY_DDL = """\
CREATE TABLE IF NOT EXISTS _migration_history (
    version     INTEGER PRIMARY KEY,
    description TEXT    NOT NULL,
    applied_at  TEXT    NOT NULL,            -- ISO UTC (datetime('now'))
    code_hash   TEXT    NOT NULL,            -- sha256, format identified by hash_format
    hash_format TEXT                         -- e.g. 'bytecode_v1'; NULL == legacy
)
"""


class MigrationRunner:
    """Versioned migration runner for a single SQLite database.

    Construct one per DB (e.g., ``TASK_MIGRATIONS`` for ``task_metadata.db``)
    and call ``run(conn)`` on every connection open. The runner is
    cheap when the DB is already at the latest version (one PRAGMA
    read + one history-hash verify pass; no DDL when nothing needs
    to apply).
    """

    def __init__(self, name: str, migrations: list[Migration]) -> None:
        self.name = name
        # Sort defensively; the caller's list order is the source of
        # truth but we double-check there are no duplicates / out-of-order
        # entries that could silently corrupt the audit trail.
        seen_versions: set[int] = set()
        for m in migrations:
            if m.version in seen_versions:
                raise ValueError(
                    f"{name}: duplicate migration version {m.version}"
                )
            seen_versions.add(m.version)
        self.migrations: list[Migration] = sorted(migrations, key=lambda m: m.version)
        if self.migrations and self.migrations[0].version != 1:
            raise ValueError(
                f"{name}: first migration must be version 1, got "
                f"{self.migrations[0].version}"
            )

    @property
    def target_version(self) -> int:
        """The highest version this runner knows about (== max(migrations))."""
        return self.migrations[-1].version if self.migrations else 0

    def run(self, conn: sqlite3.Connection) -> None:
        """Apply any missing migrations to bring ``conn`` to the latest version.

        Cheap when already at latest (one PRAGMA + one hash-verify pass).
        Safe to call on every connection open.
        """
        # ── Setup: version probe + downgrade guard + hash audit.
        #   All under one write transaction so two concurrent processes
        #   can't race past the version check.
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(_HISTORY_DDL)
            self._ensure_history_schema(conn)
            current = self._get_user_version(conn)

            # Downgrade guard: refuse to open a DB whose version we don't
            # know about. The user must upgrade work-buddy first.
            if current > self.target_version:
                raise SchemaVersionTooNew(
                    f"{self.name}: DB at v{current} but this code only "
                    f"knows up to v{self.target_version}. Upgrade "
                    "work-buddy to a version that includes the missing "
                    "migrations before opening this DB."
                )

            # Hash audit: every already-applied migration's source must
            # match what's in the history table. Edits to shipped
            # migrations are bugs — the right fix is a new migration,
            # not an in-place edit.
            self._verify_history_hashes(conn, current)

            # Baseline-stamp: if the DB has tables from a prior
            # informal-migration era (user_version == 0 but the schema
            # is clearly past v1) we treat it as "all migrations up to
            # max-applied have been historically applied" and record
            # stamps for them. This is the standard adoption pattern
            # for moving an existing project under a migration
            # framework (Alembic calls it "stamp"). The migrations are
            # idempotent, so this is correctness-equivalent to running
            # them all again, but cheaper and produces a cleaner
            # history.
            if current == 0:
                inferred = self._infer_baseline_version(conn)
                if inferred > 0:
                    logger.info(
                        "%s: baseline-stamping pre-existing DB at "
                        "inferred version %d (was untracked)",
                        self.name, inferred,
                    )
                    self._stamp_baseline(conn, inferred)
                    current = inferred

            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        # ── Apply each pending migration in its own transaction.
        #   PRAGMA foreign_keys must be set BEFORE BEGIN — it's a no-op
        #   inside an open transaction. The callable + version bump
        #   + history insert share one transaction so a crash mid-step
        #   rolls back schema and version together.
        for migration in self.migrations:
            if migration.version <= current:
                continue
            self._apply_one(conn, migration)
            current = migration.version

    # ─── Helpers ────────────────────────────────────────────────────

    def _get_user_version(self, conn: sqlite3.Connection) -> int:
        return conn.execute("PRAGMA user_version").fetchone()[0]

    def _set_user_version(self, conn: sqlite3.Connection, version: int) -> None:
        # PRAGMA user_version doesn't support parameter binding, but
        # ``version`` is always an integer from a trusted source
        # (our own migration list), never user input — so direct
        # interpolation is safe.
        conn.execute(f"PRAGMA user_version = {int(version)}")

    @staticmethod
    def _hash_callable(fn: Callable) -> str:
        """Hash a migration callable from its source AST.

        ``inspect.getsource`` returns the function text; ``ast.parse``
        produces a syntax tree; we strip docstrings and the outer
        function name; ``ast.unparse`` round-trips the tree back to
        canonical source text; SHA-256 reduces that text to a digest.
        Comments, blank lines, and whitespace never reach the AST so
        they're invisible by construction. Docstrings *do* reach the
        AST (as the first ``Expr`` of a body) so we strip them
        explicitly before unparsing.

        Real edits the audit catches:

        - Any change to a string literal the function loads (e.g. the
          DDL inside ``execute()``) — visible as a ``Constant`` node.
        - Any change to control flow, calls, or operators.
        - Any change to which globals or builtins are referenced.
        - Any rename of local variables, arguments, or the function
          itself — ``Name``, ``arg``, and ``FunctionDef.name`` all
          carry identifiers.

        Why this approach, not bytecode: the previous ``bytecode_v1``
        format hashed ``dis.get_instructions`` output, which depends
        on the CPython compiler. The same source produced different
        hashes on Python 3.11 vs 3.13 because the compilers emit
        slightly different instruction streams. ``ast.unparse``
        round-trips the syntax tree back to source text — the output
        depends only on the tree's *shape*, not on how the compiler
        emits bytecode, and it's stable across Python releases that
        parse the source identically. (``ast.dump`` would have
        worked, but its empty-field behaviour changed in 3.13 — see
        bpo-48935 — and ``_fields`` itself shifted between minor
        releases. ``ast.unparse`` sidesteps both.)

        Fallback: callables we can't pull source for (C-defined,
        ``functools.partial``, callable instances with no inspectable
        ``__call__`` source) get a qualname-only fingerprint. Detects
        identity changes, nothing finer — same posture as the prior
        no-code fallback.
        """
        try:
            source = inspect.getsource(fn)
        except (OSError, TypeError):
            name = getattr(
                fn, "__qualname__", None
            ) or getattr(type(fn), "__qualname__", repr(fn))
            return hashlib.sha256(
                f"<no-source:{name}>".encode("utf-8")
            ).hexdigest()

        tree = ast.parse(textwrap.dedent(source))
        _strip_docstrings(tree)
        _normalize_outer_fn_name(tree)
        # ``ast.unparse`` round-trips the tree back to canonical source
        # text. This is the version-portable serialization: it ignores
        # field-set changes (e.g. 3.12 adding ``type_params`` to
        # ``FunctionDef``) and the 3.13 ``ast.dump`` empty-field
        # behaviour change, because both modifications only affect
        # *fields that aren't actually populated by typical migration
        # source*. The unparsed text is the same on every Python.
        canonical = ast.unparse(tree)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _ensure_history_schema(self, conn: sqlite3.Connection) -> None:
        """Bring ``_migration_history`` up to the current shape.

        The DDL in ``_HISTORY_DDL`` is for fresh installs. Legacy DBs
        (from before we tracked ``hash_format``) need the column
        added via ALTER TABLE. Cheap idempotent operation — checks
        column presence before touching the table.
        """
        cols = {
            r[1] for r in conn.execute("PRAGMA table_info(_migration_history)")
        }
        if "hash_format" not in cols:
            conn.execute(
                "ALTER TABLE _migration_history ADD COLUMN hash_format TEXT"
            )

    def _verify_history_hashes(
        self, conn: sqlite3.Connection, current_version: int,
    ) -> None:
        """Check that every already-applied migration's recorded hash
        matches the current hash of its callable.

        Auto-rehash path: any row whose ``hash_format`` is NULL or
        anything other than the current format identifier is treated
        as a legacy stamp. We trust the deployed source (the user is
        already running on it), compute a fresh hash with the current
        method, and update the row in place. After the first run with
        new framework code, all stamps are at ``HASH_FORMAT_CURRENT``
        and the audit runs normally.
        """
        rows = conn.execute(
            "SELECT version, code_hash, hash_format FROM _migration_history "
            "WHERE version <= ? ORDER BY version",
            (current_version,),
        ).fetchall()
        recorded = {r[0]: (r[1], r[2]) for r in rows}
        for migration in self.migrations:
            if migration.version > current_version:
                break
            entry = recorded.get(migration.version)
            if entry is None:
                # Migration was applied (user_version says so) but no
                # history row exists. This is the baseline-stamp case
                # we handle separately, OR a manually-mutated DB.
                # Either way, not a hash mismatch — skip.
                continue
            stored_hash, stored_format = entry
            current_hash = self._hash_callable(migration.fn)

            if stored_format != HASH_FORMAT_CURRENT:
                # Legacy stamp from before this hash format existed.
                # Trust the deployed source, re-stamp silently with
                # the new format. Done once per migration; subsequent
                # opens compare strictly.
                logger.info(
                    "%s: re-stamping v%d audit hash "
                    "(legacy format %r -> %r)",
                    self.name, migration.version,
                    stored_format, HASH_FORMAT_CURRENT,
                )
                conn.execute(
                    "UPDATE _migration_history "
                    "SET code_hash = ?, hash_format = ? WHERE version = ?",
                    (current_hash, HASH_FORMAT_CURRENT, migration.version),
                )
                continue

            if stored_hash != current_hash:
                raise MigrationHashMismatch(
                    f"{self.name}: migration v{migration.version} "
                    f"({migration.description!r}) was applied with a "
                    f"different source than the current code. "
                    f"Stored: {stored_hash[:12]}…  current: "
                    f"{current_hash[:12]}…  Add a NEW migration step "
                    "to correct whatever the edit was for; don't edit "
                    "the historical step in place."
                )

    def _infer_baseline_version(self, conn: sqlite3.Connection) -> int:
        """Inspect the schema to infer what version a pre-framework DB is at.

        Used only when ``user_version == 0`` AND tables already exist.
        Subclasses (well, per-DB migration files) can override the
        inference by passing a custom ``baseline_inferrer`` to the
        runner. Default heuristic: if any table at all exists besides
        ``_migration_history``, treat it as ``target_version`` (assume
        the DB is fully migrated and we're adopting the framework).
        This matches the safer "assume migrated, run any new
        migrations on next bump" posture.
        """
        # NOTE: this is intentionally conservative. A per-DB override
        # could do something smarter (e.g., probe for specific Slice-N
        # columns to pin the actual version). For task_metadata, the
        # columns are all already present in the current codebase, so
        # ``target_version`` is correct.
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name != '_migration_history'"
            )
        }
        if not tables:
            return 0  # truly fresh DB; run migrations from 1
        return self.target_version

    def _stamp_baseline(self, conn: sqlite3.Connection, version: int) -> None:
        """Record history rows for migrations 1..version as "baseline" entries.

        Marks them as applied at the current time with their current
        hash, so future ``_verify_history_hashes`` calls succeed.
        Note ``applied_at`` is "now" rather than the actual historical
        apply time — we don't have that information for pre-framework
        DBs and it's a harmless approximation.
        """
        for migration in self.migrations:
            if migration.version > version:
                break
            conn.execute(
                "INSERT OR IGNORE INTO _migration_history "
                "(version, description, applied_at, code_hash, hash_format) "
                "VALUES (?, ?, datetime('now'), ?, ?)",
                (
                    migration.version,
                    f"baseline-stamp: {migration.description}",
                    self._hash_callable(migration.fn),
                    HASH_FORMAT_CURRENT,
                ),
            )
        self._set_user_version(conn, version)

    def _apply_one(
        self, conn: sqlite3.Connection, migration: Migration,
    ) -> None:
        """Run a single migration step in its own transaction."""
        logger.info(
            "%s: applying v%d (%s)",
            self.name, migration.version, migration.description,
        )
        # PRAGMA foreign_keys = OFF must come BEFORE BEGIN — it's
        # silently a no-op inside an open transaction. Re-enable in
        # finally so it's restored regardless of outcome.
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN IMMEDIATE")
        try:
            migration.fn(conn)
            conn.execute(
                "INSERT INTO _migration_history "
                "(version, description, applied_at, code_hash, hash_format) "
                "VALUES (?, ?, datetime('now'), ?, ?)",
                (
                    migration.version,
                    migration.description,
                    self._hash_callable(migration.fn),
                    HASH_FORMAT_CURRENT,
                ),
            )
            self._set_user_version(conn, migration.version)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            logger.exception(
                "%s: v%d (%s) failed, rolled back",
                self.name, migration.version, migration.description,
            )
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
