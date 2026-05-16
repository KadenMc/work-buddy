"""Migration ladder for ``task_metadata.db``.

Each step here is a discrete, idempotent DDL change. The ladder is the
authoritative history of how the task store's schema evolved — fresh
installs run all of them to reproduce the current schema; established
DBs that pre-date this framework are baseline-stamped to the current
``target_version`` on first encounter (see
``work_buddy.storage.migrations.MigrationRunner`` for the mechanics).

## Invariants every migration here must hold

- **Idempotent.** Running on a DB that already has the resulting schema
  must be a no-op. Use ``CREATE TABLE IF NOT EXISTS``; gate
  ``ALTER TABLE ADD COLUMN`` on a ``PRAGMA table_info`` membership
  check; etc. The runner won't re-invoke an already-applied migration
  *normally*, but baseline-stamp adoption and recovery scenarios mean
  callables must tolerate being re-run.

- **One logical change per migration.** The runner's hash audit and
  audit-trail readability both assume each version corresponds to a
  single coherent change. Bundles get split into separate numbered
  steps.

- **Never edit a shipped migration callable.** The runner hashes each
  callable's source at apply time and verifies the hash on subsequent
  runs (Flyway-style). Edits to historical callables raise
  ``MigrationHashMismatch``. If a shipped migration had a bug, add a
  NEW migration step that corrects whatever it did wrong.

## History

- v1 — initial schema (foundational columns + ``task_state_history``,
  ``task_sessions``, ``task_tags`` + their indexes).
- v2 — GTD vocabulary columns on ``task_metadata`` (12 cols).
- v3 — ``description`` column.
- v4 — ``risk_profile_json`` + ``automation_tier_achievable``
  + ``last_actor`` columns.
- v5 — ``agent_required_contexts`` / ``user_required_contexts``
  / ``required_contexts_source`` columns.
- v6 — ``task_action_items`` table + ``current_action_item_id``
  column on ``task_metadata``.
- v7 — ``task_sync_status`` single-row freshness-audit table.
- v8 — ``deleted_at`` columns for soft-delete (``task_metadata``,
  ``task_action_items``, ``task_tags``).
- v9 — drop ``ON DELETE CASCADE`` from ``task_action_items`` and
  ``task_tags`` FK constraints (forces soft-delete discipline at the
  schema level).
- v10 — ``lww_meta`` append-only write-provenance sidecar backing the
  MarkdownDB last-write-wins log (see ``architecture/markdown-db``).
"""

from __future__ import annotations

import sqlite3

from work_buddy.logging_config import get_logger
from work_buddy.storage.migrations import Migration, MigrationRunner

logger = get_logger(__name__)


# ─── Helpers ────────────────────────────────────────────────────────


def _columns_of(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the set of column names currently on ``table``."""
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column_if_missing(
    conn: sqlite3.Connection,
    table: str,
    name: str,
    sql_type: str,
    default: str | None = None,
    not_null: bool = False,
) -> None:
    """Idempotent ``ALTER TABLE ADD COLUMN``.

    Default-and-not-null is required when adding a NOT NULL column to a
    table with pre-existing rows — SQLite needs a default to backfill.
    Default-only (no not_null) leaves the column nullable but with a
    default for new inserts. Nullable-no-default produces a column that
    starts NULL on existing rows.
    """
    if name in _columns_of(conn, table):
        return
    clause = f"{name} {sql_type}"
    if default is not None:
        clause += f" DEFAULT {default}"
        if not_null:
            clause += " NOT NULL"
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {clause}")


# ─── v1: initial schema ─────────────────────────────────────────────


def _m001_initial(conn: sqlite3.Connection) -> None:
    """Initial schema: task_metadata (pre-Slice-2 columns) + secondary tables.

    Captures the schema as it stood before any Slice-N evolution.
    Subsequent migrations add columns and tables on top.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_metadata (
            task_id      TEXT PRIMARY KEY,
            state        TEXT NOT NULL DEFAULT 'inbox',
            urgency      TEXT NOT NULL DEFAULT 'medium',
            complexity   TEXT,
            contract     TEXT,
            note_uuid    TEXT,
            snooze_until TEXT,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            completed_at TEXT,
            archived_at  TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_state_history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id    TEXT NOT NULL,
            old_state  TEXT,
            new_state  TEXT NOT NULL,
            changed_at TEXT NOT NULL,
            reason     TEXT,
            FOREIGN KEY (task_id) REFERENCES task_metadata(task_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_sessions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id     TEXT NOT NULL,
            session_id  TEXT NOT NULL,
            assigned_at TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES task_metadata(task_id),
            UNIQUE(task_id, session_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_tags (
            task_id      TEXT NOT NULL,
            tag          TEXT NOT NULL,
            is_namespace INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (task_id, tag),
            FOREIGN KEY (task_id) REFERENCES task_metadata(task_id) ON DELETE CASCADE
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_state    ON task_metadata(state)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_contract ON task_metadata(contract)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_history  ON task_state_history(task_id, changed_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_sessions_task    ON task_sessions(task_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_sessions_session ON task_sessions(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_tags_tag ON task_tags(tag)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_tags_ns  ON task_tags(is_namespace, tag)")


# ─── v2: Slice-2 GTD vocabulary columns ─────────────────────────────


def _m002_slice2_gtd(conn: sqlite3.Connection) -> None:
    """Slice-2: GTD task vocabulary columns on ``task_metadata`` (12 cols).

    ``creation_provenance`` is intentionally an open enum (no validator)
    so new sources can register their own provenance strings without a
    code change.
    """
    _add_column_if_missing(conn, "task_metadata", "task_kind",            "TEXT",    "'task'",       not_null=True)
    _add_column_if_missing(conn, "task_metadata", "density",              "TEXT",    "'sparse'",     not_null=True)
    _add_column_if_missing(conn, "task_metadata", "outcome_text",         "TEXT")
    _add_column_if_missing(conn, "task_metadata", "next_action_text",     "TEXT")
    _add_column_if_missing(conn, "task_metadata", "definition_of_done",   "TEXT")
    _add_column_if_missing(conn, "task_metadata", "creation_effort",      "TEXT",    "'developed'",  not_null=True)
    _add_column_if_missing(conn, "task_metadata", "user_involvement",     "TEXT",    "'high'",       not_null=True)
    _add_column_if_missing(conn, "task_metadata", "creation_provenance",  "TEXT",    "'manual'",     not_null=True)
    _add_column_if_missing(conn, "task_metadata", "has_deadline",         "INTEGER", "0",            not_null=True)
    _add_column_if_missing(conn, "task_metadata", "deadline_date",        "TEXT")
    _add_column_if_missing(conn, "task_metadata", "has_dependency",       "INTEGER", "0",            not_null=True)
    _add_column_if_missing(conn, "task_metadata", "dependency_hint",      "TEXT")


# ─── v3: Slice-3 description column ────────────────────────────────


def _m003_description(conn: sqlite3.Connection) -> None:
    """Slice-3: human-readable description column on ``task_metadata``.

    Backfilled by ``task_sync`` from the master-list markdown line.
    Source of truth is the markdown line (file follows store on
    ``task_sync``'s description-drift reconciliation path).
    """
    _add_column_if_missing(conn, "task_metadata", "description", "TEXT")


# ─── v4: Slice-4 risk + tier + last-actor ──────────────────────────


def _m004_risk_tier_actor(conn: sqlite3.Connection) -> None:
    """Slice-4: risk model + automation-tier cache + last-actor.

    All nullable. ``risk_profile_json`` is a JSON blob of the four
    risk dimensions + three amplifiers; populated by the Clarify prompt
    at task-proposal time. ``automation_tier_achievable`` caches a pure
    function of the task. ``last_actor`` is detected at mutation time
    via ``consent.get_consent_context_info()``.
    """
    _add_column_if_missing(conn, "task_metadata", "risk_profile_json",          "TEXT")
    _add_column_if_missing(conn, "task_metadata", "automation_tier_achievable", "INTEGER")
    _add_column_if_missing(conn, "task_metadata", "last_actor",                 "TEXT")


# ─── v5: Slice-5a action-context resolution layer ──────────────────


def _m005_context_arrays(conn: sqlite3.Connection) -> None:
    """Slice-5a: required-contexts JSON arrays + provenance.

    ``agent_required_contexts`` / ``user_required_contexts`` are JSON
    arrays of context tokens (e.g. ``@filesystem``, ``@email_send``).
    ``required_contexts_source`` carries provenance — flips to
    ``user_authored`` once a human edits the inferred set so future
    Clarify re-runs don't clobber the edit.
    """
    _add_column_if_missing(conn, "task_metadata", "agent_required_contexts",  "TEXT")
    _add_column_if_missing(conn, "task_metadata", "user_required_contexts",   "TEXT")
    _add_column_if_missing(conn, "task_metadata", "required_contexts_source", "TEXT")


# ─── v6: Slice-7 action items table + per-task current pointer ─────


def _m006_action_items(conn: sqlite3.Connection) -> None:
    """task_action_items table + current_action_item_id pointer.

    Tasks can have an ordered list of action items (steps). Each item
    carries its own risk profile + required contexts + DoD, mirroring
    the parent ``task_metadata`` shape. ``current_action_item_id``
    on ``task_metadata`` points at the step the user is currently
    focused on.

    The FK on ``task_action_items.task_id`` is declared here with
    ``ON DELETE CASCADE``; v9 drops the cascade via a table-rebuild
    once the soft-delete columns added in v8 make raw DELETE paths
    obsolete.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_action_items (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id                  TEXT NOT NULL,
            sequence                 INTEGER NOT NULL,
            description              TEXT NOT NULL,
            state                    TEXT NOT NULL DEFAULT 'pending',
            risk_profile_json        TEXT,
            agent_required_contexts  TEXT,
            user_required_contexts   TEXT,
            definition_of_done       TEXT,
            user_authored            INTEGER NOT NULL DEFAULT 0,
            approved_at              TEXT,
            completed_at             TEXT,
            handoff_package_path     TEXT,
            created_at               TEXT NOT NULL,
            updated_at               TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES task_metadata(task_id) ON DELETE CASCADE,
            UNIQUE(task_id, sequence)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_action_items_task  ON task_action_items(task_id, sequence)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_action_items_state ON task_action_items(state)")
    _add_column_if_missing(conn, "task_metadata", "current_action_item_id", "INTEGER")


# ─── v7: task_sync_status freshness audit ──────────────────────────


def _m007_task_sync_status(conn: sqlite3.Connection) -> None:
    """``task_sync_status`` single-row freshness audit table.

    The dashboard reads ``last_full_sync_at`` to render the
    "synced Xm ago" label. Single-row enforced by ``CHECK (id = 1)``.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_sync_status (
            id                INTEGER PRIMARY KEY CHECK (id = 1),
            last_full_sync_at TEXT,
            last_sync_created INTEGER NOT NULL DEFAULT 0,
            last_sync_updated INTEGER NOT NULL DEFAULT 0,
            last_sync_deleted INTEGER NOT NULL DEFAULT 0,
            updated_at        TEXT NOT NULL
        )
    """)


# ─── v8: soft-delete columns ───────────────────────────────────────


def _m008_soft_delete(conn: sqlite3.Connection) -> None:
    """``deleted_at`` columns for soft-delete on ``task_metadata``
    and ``task_action_items``.

    Hard-DELETE against ``task_metadata`` is a wide-fanout vector
    class: a single buggy upstream call (e.g. a regex that collapses
    multiple file IDs into one match) can destroy many rows along
    with their cascaded children. Soft-delete closes that vector at
    the schema level — the row is flagged with a timestamp, query
    paths default-filter to hide flagged rows, and ``store.restore()``
    clears the flag.

    See ``architecture/backups`` and ``tasks/task_delete`` for the
    broader safety architecture.

    Nullable column on both tables — existing rows start with NULL
    ``deleted_at`` meaning "still live." Query layer's default filter
    is ``WHERE deleted_at IS NULL``.
    """
    _add_column_if_missing(conn, "task_metadata",      "deleted_at", "TEXT")
    _add_column_if_missing(conn, "task_action_items",  "deleted_at", "TEXT")


# ─── v9: drop ON DELETE CASCADE from FKs ───────────────────────────


def _m009_drop_cascade(conn: sqlite3.Connection) -> None:
    """Rebuild ``task_action_items`` and ``task_tags`` without
    ``ON DELETE CASCADE``, AND reconcile any lingering schema
    divergence on ``task_action_items``.

    SQLite doesn't support ``ALTER TABLE ... DROP CONSTRAINT``, so
    constraint changes need the canonical table-rebuild pattern:
    CREATE NEW → INSERT FROM OLD → DROP OLD → RENAME NEW → recreate
    indexes → integrity check.

    With soft-delete (v8) there are no ``DELETE FROM task_metadata``
    paths in production code; without the cascade, any future code
    path that bypasses the soft-delete API and attempts a raw DELETE
    will be REJECTED by SQLite (FK action defaults to ``NO ACTION``,
    which refuses parent delete with referencing children — exactly
    the safety property we want).

    **Schema reconciliation:** an earlier refactor replaced the
    ``user_authored INTEGER + approved_at TEXT`` pair on
    ``task_action_items`` with a single ``authorship TEXT`` enum
    (values: ``'user'`` / ``'agent_approved'`` / ``'agent_unapproved'``).
    Some live DBs already have the new schema (carried by a baseline-
    stamp adoption); a fresh install walking 1..6 ends with the OLD
    columns. m009 detects the actual layout via ``PRAGMA table_info``
    and:

    - If ``authorship`` is present: rebuild with the same columns,
      changing only the FK action.
    - If ``user_authored`` is present: rebuild with ``authorship``,
      mapping the old values:
        ``user_authored = 1`` → ``'user'``
        ``user_authored = 0 AND approved_at IS NOT NULL`` → ``'agent_approved'``
        ``user_authored = 0 AND approved_at IS NULL`` → ``'agent_unapproved'``

    The migration is idempotent: it inspects the existing FK clauses
    via ``PRAGMA foreign_key_list`` and skips the rebuild if the
    cascade is already absent.

    ``PRAGMA foreign_keys = OFF`` is set by the runner BEFORE
    ``BEGIN``; ``PRAGMA foreign_key_check`` + ``PRAGMA integrity_check``
    run INSIDE the transaction before the implicit commit so any
    violation rolls back the rebuild.
    """
    _rebuild_task_action_items_without_cascade(conn)
    _rebuild_task_tags_without_cascade(conn)

    # ── Integrity check: catches real corruption (e.g. dropped indexes
    #    that the rebuild forgot to recreate). Hard failure if not "ok".
    integrity = [tuple(r) for r in conn.execute("PRAGMA integrity_check")]
    if integrity != [("ok",)]:
        raise RuntimeError(f"m009 integrity_check failed: {integrity}")

    # ── Foreign-key check: SOFT here. ``PRAGMA foreign_key_check`` runs
    #    against the WHOLE DB and reports any row whose FK column points
    #    at a non-existent parent. We use it as a diagnostic, NOT a
    #    gating check — the migration's job is to drop CASCADE, and
    #    pre-existing orphans (from earlier incidents, from dev test
    #    data like t-LIVE-DEV-001, etc.) are a SEPARATE cleanup
    #    concern. Cleaning them inside this migration would force a
    #    data-destruction call we don't have user consent for. Log a
    #    warning at WARN level with the orphan counts per table; the
    #    rebuild itself proceeds.
    violations = [tuple(r) for r in conn.execute("PRAGMA foreign_key_check")]
    if violations:
        by_table: dict[str, int] = {}
        for v in violations:
            by_table[v[0]] = by_table.get(v[0], 0) + 1
        logger.warning(
            "m009: %d pre-existing FK violation(s) detected across child "
            "tables (NOT caused by this migration): %s. These are "
            "orphan rows referencing task_metadata IDs that no longer "
            "exist (likely from old test data or post-incident state). "
            "Migration completes regardless. Add a cleanup follow-up "
            "task to reconcile.",
            len(violations), by_table,
        )


def _fk_has_cascade_on_delete(
    conn: sqlite3.Connection, table: str, column: str,
) -> bool:
    """True if ``table.column`` is a FK with ``ON DELETE CASCADE``.

    ``PRAGMA foreign_key_list`` returns rows of the form
    ``(id, seq, table, from, to, on_update, on_delete, match)``.
    """
    for row in conn.execute(f"PRAGMA foreign_key_list({table})"):
        if row[3] == column and row[6] == "CASCADE":
            return True
    return False


def _rebuild_task_action_items_without_cascade(conn: sqlite3.Connection) -> None:
    """Rebuild ``task_action_items`` with FK ``NO ACTION`` + reconcile
    the legacy ``user_authored``/``approved_at`` columns into the
    canonical ``authorship`` TEXT enum if either legacy column is
    still present.

    Idempotent: skips the rebuild if the cascade is already gone.
    """
    if not _fk_has_cascade_on_delete(conn, "task_action_items", "task_id"):
        return  # already rebuilt — idempotent no-op

    # Always rebuild WITH ``authorship`` as the canonical column;
    # detect the source schema and shape the SELECT accordingly.
    src_cols = _columns_of(conn, "task_action_items")
    has_authorship = "authorship" in src_cols
    has_legacy = "user_authored" in src_cols  # also implies approved_at

    conn.execute("""
        CREATE TABLE task_action_items__new (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id                  TEXT NOT NULL,
            sequence                 INTEGER NOT NULL,
            description              TEXT NOT NULL,
            state                    TEXT NOT NULL DEFAULT 'pending',
            risk_profile_json        TEXT,
            agent_required_contexts  TEXT,
            user_required_contexts   TEXT,
            definition_of_done       TEXT,
            authorship               TEXT NOT NULL DEFAULT 'agent_unapproved',
            completed_at             TEXT,
            handoff_package_path     TEXT,
            created_at               TEXT NOT NULL,
            updated_at               TEXT NOT NULL,
            deleted_at               TEXT,
            FOREIGN KEY (task_id) REFERENCES task_metadata(task_id),
            UNIQUE(task_id, sequence)
        )
    """)

    # Source SELECT shape depends on which schema the existing table
    # has. The destination shape is always the same.
    if has_authorship:
        conn.execute("""
            INSERT INTO task_action_items__new (
                id, task_id, sequence, description, state, risk_profile_json,
                agent_required_contexts, user_required_contexts,
                definition_of_done, authorship, completed_at,
                handoff_package_path, created_at, updated_at, deleted_at
            )
            SELECT
                id, task_id, sequence, description, state, risk_profile_json,
                agent_required_contexts, user_required_contexts,
                definition_of_done, authorship, completed_at,
                handoff_package_path, created_at, updated_at, deleted_at
            FROM task_action_items
        """)
    elif has_legacy:
        # Translate user_authored/approved_at into the authorship enum.
        conn.execute("""
            INSERT INTO task_action_items__new (
                id, task_id, sequence, description, state, risk_profile_json,
                agent_required_contexts, user_required_contexts,
                definition_of_done, authorship, completed_at,
                handoff_package_path, created_at, updated_at, deleted_at
            )
            SELECT
                id, task_id, sequence, description, state, risk_profile_json,
                agent_required_contexts, user_required_contexts,
                definition_of_done,
                CASE
                    WHEN user_authored = 1               THEN 'user'
                    WHEN approved_at IS NOT NULL         THEN 'agent_approved'
                    ELSE                                      'agent_unapproved'
                END AS authorship,
                completed_at,
                handoff_package_path, created_at, updated_at, deleted_at
            FROM task_action_items
        """)
    else:
        # Neither schema present — table has unknown structure. Bail
        # rather than corrupt data.
        raise RuntimeError(
            f"m009: task_action_items has neither 'authorship' nor "
            f"'user_authored' columns. Found: {sorted(src_cols)}. "
            "Refusing to rebuild without a known source schema."
        )

    conn.execute("DROP TABLE task_action_items")
    conn.execute("ALTER TABLE task_action_items__new RENAME TO task_action_items")
    # Indexes were dropped with the old table; recreate them.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_action_items_task "
        "ON task_action_items(task_id, sequence)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_action_items_state "
        "ON task_action_items(state)"
    )


def _rebuild_task_tags_without_cascade(conn: sqlite3.Connection) -> None:
    """Rebuild ``task_tags`` with FK ``NO ACTION``. Idempotent skip if
    already done. Column list pinned to the m009 schema (task_tags has
    not gained columns since m001)."""
    if not _fk_has_cascade_on_delete(conn, "task_tags", "task_id"):
        return  # already rebuilt — idempotent no-op

    conn.execute("""
        CREATE TABLE task_tags__new (
            task_id      TEXT NOT NULL,
            tag          TEXT NOT NULL,
            is_namespace INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (task_id, tag),
            FOREIGN KEY (task_id) REFERENCES task_metadata(task_id)
        )
    """)
    conn.execute("""
        INSERT INTO task_tags__new (task_id, tag, is_namespace)
        SELECT task_id, tag, is_namespace FROM task_tags
    """)
    conn.execute("DROP TABLE task_tags")
    conn.execute("ALTER TABLE task_tags__new RENAME TO task_tags")
    # Recreate indexes.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_tags_tag ON task_tags(tag)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_tags_ns "
        "ON task_tags(is_namespace, tag)"
    )


# ─── v10: lww_meta sidecar (MarkdownDB last-write-wins log) ──────────


def _m010_lww_meta(conn: sqlite3.Connection) -> None:
    """Create the ``lww_meta`` append-only write-provenance table.

    Backs :class:`work_buddy.markdown_db.SqliteLwwLog`. Co-located in
    ``task_metadata.db`` so it travels with backups + restores. Every
    write through a :class:`~work_buddy.markdown_db.MarkdownDB` appends
    one row per field per surface; nothing is ever updated or deleted.

    The DDL is inlined here (rather than imported from
    ``markdown_db.sqlite_lww``) deliberately: the migration runner
    hashes this callable's source, so the schema this step installs must
    be visible in the callable for the audit to catch any change.
    Keep it byte-identical to ``markdown_db.sqlite_lww.LWW_META_DDL``.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS lww_meta (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            table_name    TEXT NOT NULL,
            row_pk        TEXT NOT NULL,
            field         TEXT NOT NULL,
            ts            TEXT NOT NULL,
            actor         TEXT NOT NULL DEFAULT '[]',
            process       TEXT NOT NULL,
            from_surface  TEXT,
            to_surface    TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_lww_meta_latest
            ON lww_meta(table_name, row_pk, field, to_surface, ts);
    """)


# ─── Runner ─────────────────────────────────────────────────────────


TASK_MIGRATIONS = MigrationRunner("task_metadata", migrations=[
    Migration(1, "initial schema",                       _m001_initial),
    Migration(2, "GTD vocabulary columns",               _m002_slice2_gtd),
    Migration(3, "description column",                   _m003_description),
    Migration(4, "risk profile + tier + last_actor",     _m004_risk_tier_actor),
    Migration(5, "agent/user required context arrays",   _m005_context_arrays),
    Migration(6, "action_items table + current pointer", _m006_action_items),
    Migration(7, "task_sync_status freshness table",     _m007_task_sync_status),
    Migration(8, "soft-delete deleted_at columns",       _m008_soft_delete),
    Migration(9, "drop ON DELETE CASCADE from FKs",      _m009_drop_cascade),
    Migration(10, "lww_meta write-provenance sidecar",   _m010_lww_meta),
])
