"""SQLite task metadata store — external storage for task attributes.

The markdown task line stays clean (just #todo, text, #projects/*, 🆔, and
plugin emojis). All work-buddy metadata (state, urgency, complexity,
contract link, review dates, state history) lives here, keyed by task ID.

The store is the source of truth for work-buddy metadata. The Obsidian Tasks
plugin cache is the source of truth for plugin-owned data (checkbox, dates,
priority emojis). They don't overlap.

Schema follows the messaging/models.py pattern: SQLite with WAL mode,
row_factory=sqlite3.Row, auto-create on first access.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from work_buddy.config import load_config
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS task_metadata (
    task_id         TEXT PRIMARY KEY,   -- e.g. 't-a3f8c1e2'
    state           TEXT NOT NULL DEFAULT 'inbox',
    urgency         TEXT NOT NULL DEFAULT 'medium',
    complexity      TEXT,               -- 'simple', 'moderate', 'complex', or NULL
    contract        TEXT,               -- contract slug this task serves, or NULL
    note_uuid       TEXT,               -- UUID of linked note file, or NULL
    snooze_until    TEXT,               -- ISO date to wake snoozed task, or NULL
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    completed_at    TEXT,               -- ISO timestamp when state became 'done'
    archived_at     TEXT,               -- ISO timestamp when moved to archive
    -- Slice 2: GTD vocabulary additions ---------------------------
    task_kind       TEXT NOT NULL DEFAULT 'task',  -- 'task' | 'periodic' | 'habit'
    density         TEXT NOT NULL DEFAULT 'sparse',-- 'sparse' | 'developed' | 'dense'
    outcome_text    TEXT,               -- desired end-state for developed tasks
    next_action_text TEXT,              -- specific physical action for developed tasks
    definition_of_done TEXT,            -- closing signal
    creation_effort TEXT NOT NULL DEFAULT 'developed',  -- 'sparse' | 'medium' | 'developed'
    user_involvement TEXT NOT NULL DEFAULT 'high',      -- 'low' | 'medium' | 'high'
    creation_provenance TEXT NOT NULL DEFAULT 'manual', -- 'manual' | 'agent_inferred_from_journal' | …
    has_deadline    INTEGER NOT NULL DEFAULT 0,
    deadline_date   TEXT,               -- ISO date when has_deadline=1
    has_dependency  INTEGER NOT NULL DEFAULT 0,
    dependency_hint TEXT,               -- free-text hint when has_dependency=1
    -- Slice 3: description column ---------------------------------
    -- Human-readable task text extracted from the master-list line
    -- (checkbox / tags / wikilink / plugin emojis / 🆔 stripped).
    -- NULL on initial migration; backfilled by task_sync from the
    -- file. Source of truth: the markdown line. Store follows file
    -- (same precedent as the checkbox/note_uuid reconciliation paths).
    description     TEXT,
    -- Slice 4: risk model + automation tiers + last-actor ---------
    -- ``risk_profile_json``: JSON blob with the four dimensions
    -- (financial, privacy, accuracy, compute) + three amplifiers
    -- (reversibility, regret_potential, inference_uncertainty).
    -- NULL = "not yet classified" — the resolver treats NULL as the
    -- safe-profile fallback (low across the board, low amplifiers).
    -- Populated by Slice 3's Clarify prompt at task-proposal time.
    risk_profile_json TEXT,
    -- ``automation_tier_achievable``: cached output of
    -- ``resolve_achievable_tier(task)``. The OPERATING tier is NOT
    -- stored — it's computed on read from achievable × allowed × risk.
    -- See ``work_buddy.automation.risk``.
    automation_tier_achievable INTEGER,
    -- ``last_actor``: 'agent' | 'user' | NULL. Detected at mutation
    -- time via ``consent.get_consent_context_info()`` — when the
    -- mutation fires inside a ``user_initiated()`` block the actor
    -- is the user; otherwise an autonomous agent path. NULL for
    -- legacy tasks created before Slice 4.
    last_actor      TEXT
);

CREATE TABLE IF NOT EXISTS task_state_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL,
    old_state       TEXT,
    new_state       TEXT NOT NULL,
    changed_at      TEXT NOT NULL,
    reason          TEXT,               -- optional: why the state changed
    FOREIGN KEY (task_id) REFERENCES task_metadata(task_id)
);

CREATE TABLE IF NOT EXISTS task_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    assigned_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES task_metadata(task_id),
    UNIQUE(task_id, session_id)
);

CREATE TABLE IF NOT EXISTS task_tags (
    task_id       TEXT NOT NULL,
    tag           TEXT NOT NULL,        -- normalized, no leading '#'
    is_namespace  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (task_id, tag),
    FOREIGN KEY (task_id) REFERENCES task_metadata(task_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_task_state
    ON task_metadata(state);
CREATE INDEX IF NOT EXISTS idx_task_contract
    ON task_metadata(contract);
CREATE INDEX IF NOT EXISTS idx_task_history
    ON task_state_history(task_id, changed_at);
CREATE INDEX IF NOT EXISTS idx_task_sessions_task
    ON task_sessions(task_id);
CREATE INDEX IF NOT EXISTS idx_task_sessions_session
    ON task_sessions(session_id);
CREATE INDEX IF NOT EXISTS idx_task_tags_tag
    ON task_tags(tag);
CREATE INDEX IF NOT EXISTS idx_task_tags_ns
    ON task_tags(is_namespace, tag);
"""

VALID_STATES = {"inbox", "mit", "focused", "snoozed", "done"}
VALID_URGENCIES = {"low", "medium", "high"}
VALID_COMPLEXITIES = {"simple", "moderate", "complex", None}

# Slice 2 enums --------------------------------------------------------------
# task_kind enum. 'periodic' and 'habit' ship as forward-compat values
# (Slice 9 wires up the reminder system that drives them); 'task' is the
# Slice 2 default.
VALID_TASK_KINDS = {"task", "periodic", "habit"}

# density enum. 'dense' is forward-compat for Slice 7+; not used in Slice 2.
VALID_DENSITIES = {"sparse", "developed", "dense"}

# creation_effort: how informed was the agent that wrote this task?
VALID_CREATION_EFFORTS = {"sparse", "medium", "developed"}

# user_involvement: was the user actively engaged or did the agent infer it?
VALID_USER_INVOLVEMENTS = {"low", "medium", "high"}

# creation_provenance is intentionally OPEN (no validator) — new sources
# (telegram, calendar, smart-source, …) get to register their own
# provenance string without a code change. The starter set is documented
# in Slice 2's task note. Convention: 'manual' or 'agent_inferred_from_*'.

# Slice 4 enums --------------------------------------------------------
# last_actor: who most recently acted on the task. NULL = legacy (no
# actor recorded yet). The set is closed because the resolver branches
# on it; if Slice 7+ wants per-action-item actors, add a separate column.
VALID_LAST_ACTORS = {"agent", "user", None}

# Slice 2 column descriptors used by the idempotent migration. Keep this
# in sync with the Slice 2 columns in _SCHEMA above. Format:
#   (column_name, sqlite_type, default_sql_literal_or_None, not_null_bool)
_SLICE_2_COLUMNS: list[tuple[str, str, str | None, bool]] = [
    ("task_kind", "TEXT", "'task'", True),
    ("density", "TEXT", "'sparse'", True),
    ("outcome_text", "TEXT", None, False),
    ("next_action_text", "TEXT", None, False),
    ("definition_of_done", "TEXT", None, False),
    ("creation_effort", "TEXT", "'developed'", True),
    ("user_involvement", "TEXT", "'high'", True),
    ("creation_provenance", "TEXT", "'manual'", True),
    ("has_deadline", "INTEGER", "0", True),
    ("deadline_date", "TEXT", None, False),
    ("has_dependency", "INTEGER", "0", True),
    ("dependency_hint", "TEXT", None, False),
]

# Slice 3 column descriptors: human-readable task description text.
# Nullable on initial migration; task_sync backfills from the master
# task list on its next run. Adding more columns later: append a new
# ``_SLICE_N_COLUMNS`` list and extend the tuple consumed by
# ``_migrate_schema``.
_SLICE_3_COLUMNS: list[tuple[str, str, str | None, bool]] = [
    ("description", "TEXT", None, False),
]

# Slice 4 column descriptors: risk model + automation-tier cache +
# last-actor.  All nullable.  ``risk_profile_json`` is a JSON blob of
# the four dimensions + three amplifiers — see
# ``work_buddy.automation.risk`` for the schema and resolver semantics.
# ``automation_tier_achievable`` caches a pure function of the task; it
# is rebuilt by Clarify on creation and may be left NULL until then
# (resolver re-derives lazily).  ``last_actor`` is detected at mutation
# time via ``consent.get_consent_context_info()`` — we DON'T migrate
# legacy rows; NULL means "before Slice 4 wired this in".
_SLICE_4_COLUMNS: list[tuple[str, str, str | None, bool]] = [
    ("risk_profile_json", "TEXT", None, False),
    ("automation_tier_achievable", "INTEGER", None, False),
    ("last_actor", "TEXT", None, False),
]

# All slice-N column lists, in migration order. Append new lists here
# rather than modifying historical ones — the comment headers above each
# list are reading material for future-you ("when did this column
# appear and why").
_ALL_MIGRATED_COLUMNS: list[tuple[str, str, str | None, bool]] = (
    _SLICE_2_COLUMNS + _SLICE_3_COLUMNS + _SLICE_4_COLUMNS
)


def _db_path() -> Path:
    """Resolve the task metadata database path from config."""
    cfg = load_config()
    custom = cfg.get("tasks", {}).get("db_path")
    if custom:
        from work_buddy.paths import repo_root
        p = Path(custom) if Path(custom).is_absolute() else repo_root() / custom
    else:
        from work_buddy.paths import resolve
        p = resolve("db/tasks")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def get_connection() -> sqlite3.Connection:
    """Open (or create) the task metadata database with WAL mode.

    On every open we run :func:`_migrate_schema` — the migration is
    idempotent (PRAGMA table_info gate) so the cost is one sqlite query
    when columns already exist. This keeps the repo's "schema is what
    you see in _SCHEMA" promise without requiring a separate migration
    runner.
    """
    path = _db_path()
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    _migrate_schema(conn)
    return conn


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Idempotent ALTER TABLE migration for schema additions.

    SQLite's ``CREATE TABLE IF NOT EXISTS`` doesn't add columns to an
    existing table. For every Slice-N column we want, check
    ``PRAGMA table_info(task_metadata)`` and ``ALTER TABLE ADD COLUMN``
    if missing. Defaults in the ALTER clause backfill existing rows.

    Slice 2 columns added: task_kind, density, outcome_text,
    next_action_text, definition_of_done, creation_effort,
    user_involvement, creation_provenance, has_deadline,
    deadline_date, has_dependency, dependency_hint.

    Slice 3 columns added: description.

    Slice 4 columns added: risk_profile_json,
    automation_tier_achievable, last_actor.

    Adding more columns later: append a new ``_SLICE_N_COLUMNS`` list
    and extend ``_ALL_MIGRATED_COLUMNS``. They'll get migrated on the
    next connection open.
    """
    existing = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(task_metadata)")
    }
    for col_name, sql_type, default_sql, not_null in _ALL_MIGRATED_COLUMNS:
        if col_name in existing:
            continue
        clause = f"{col_name} {sql_type}"
        if default_sql is not None:
            clause += f" DEFAULT {default_sql}"
            if not_null:
                clause += " NOT NULL"
        # Skipping NOT NULL when no default — SQLite allows this for
        # nullable additions, which is what the descriptor signals.
        conn.execute(f"ALTER TABLE task_metadata ADD COLUMN {clause}")
        logger.info("task_metadata: added column %s", col_name)
    conn.commit()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _SentinelType:
    """Distinguishes 'not provided' from None in update() kwargs."""
    def __repr__(self) -> str:
        return "<NOT_PROVIDED>"

_SENTINEL = _SentinelType()


# ── CRUD ────────────────────────────────────────────────────────


def create(
    task_id: str,
    state: str = "inbox",
    urgency: str = "medium",
    complexity: str | None = None,
    contract: str | None = None,
    note_uuid: str | None = None,
    *,
    # Slice 2 additions ----------------------------------------------
    task_kind: str = "task",
    density: str = "sparse",
    outcome_text: str | None = None,
    next_action_text: str | None = None,
    definition_of_done: str | None = None,
    creation_effort: str = "developed",
    user_involvement: str = "high",
    creation_provenance: str = "manual",
    has_deadline: bool = False,
    deadline_date: str | None = None,
    has_dependency: bool = False,
    dependency_hint: str | None = None,
    # Slice 3 addition -----------------------------------------------
    description: str | None = None,
    # Slice 4 additions ----------------------------------------------
    risk_profile_json: str | None = None,
    automation_tier_achievable: int | None = None,
    last_actor: str | None = None,
) -> dict[str, Any]:
    """Create a metadata record for a new task.

    Called when create_task() generates a new 🆔.

    Slice 2 fields default to the "legacy task" assumption:
    ``task_kind='task'``, ``density='sparse'``, ``creation_effort=
    'developed'``, ``user_involvement='high'``, ``creation_provenance=
    'manual'``. This matches the migration backfill so newly-created
    tasks look identical to legacy tasks unless callers explicitly
    pass different values.
    """
    if state not in VALID_STATES:
        raise ValueError(f"Invalid state {state!r}")
    if urgency not in VALID_URGENCIES:
        raise ValueError(f"Invalid urgency {urgency!r}")
    if task_kind not in VALID_TASK_KINDS:
        raise ValueError(f"Invalid task_kind {task_kind!r}")
    if density not in VALID_DENSITIES:
        raise ValueError(f"Invalid density {density!r}")
    if creation_effort not in VALID_CREATION_EFFORTS:
        raise ValueError(f"Invalid creation_effort {creation_effort!r}")
    if user_involvement not in VALID_USER_INVOLVEMENTS:
        raise ValueError(f"Invalid user_involvement {user_involvement!r}")
    if last_actor not in VALID_LAST_ACTORS:
        raise ValueError(
            f"Invalid last_actor {last_actor!r}: expected 'agent', 'user', or None"
        )

    now = _now_iso()
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO task_metadata
               (task_id, state, urgency, complexity, contract, note_uuid,
                created_at, updated_at,
                task_kind, density, outcome_text, next_action_text,
                definition_of_done, creation_effort, user_involvement,
                creation_provenance, has_deadline, deadline_date,
                has_dependency, dependency_hint,
                description,
                risk_profile_json, automation_tier_achievable, last_actor)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?,
                       ?, ?, ?)""",
            (
                task_id, state, urgency, complexity, contract, note_uuid,
                now, now,
                task_kind, density, outcome_text, next_action_text,
                definition_of_done, creation_effort, user_involvement,
                creation_provenance,
                int(bool(has_deadline)), deadline_date,
                int(bool(has_dependency)), dependency_hint,
                description,
                risk_profile_json, automation_tier_achievable, last_actor,
            ),
        )
        conn.execute(
            """INSERT INTO task_state_history
               (task_id, old_state, new_state, changed_at, reason)
               VALUES (?, NULL, ?, ?, ?)""",
            (task_id, state, now, "created"),
        )
        conn.commit()
    finally:
        conn.close()

    logger.info("Task metadata created: %s (state=%s)", task_id, state)
    return {"task_id": task_id, "state": state, "urgency": urgency}


def get(task_id: str) -> dict[str, Any] | None:
    """Get metadata for a task by ID. Returns None if not found."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM task_metadata WHERE task_id = ?", (task_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update(
    task_id: str,
    *,
    state: str | None = None,
    urgency: str | None = None,
    complexity: str | None = _SENTINEL,
    contract: str | None = _SENTINEL,
    snooze_until: str | None = _SENTINEL,
    note_uuid: str | None = _SENTINEL,
    reason: str | None = None,
    # Slice 2 additions ----------------------------------------------
    task_kind: str | None = None,
    density: str | None = None,
    outcome_text: str | None = _SENTINEL,
    next_action_text: str | None = _SENTINEL,
    definition_of_done: str | None = _SENTINEL,
    creation_effort: str | None = None,
    user_involvement: str | None = None,
    creation_provenance: str | None = None,
    has_deadline: bool | None = None,
    deadline_date: str | None = _SENTINEL,
    has_dependency: bool | None = None,
    dependency_hint: str | None = _SENTINEL,
    # Slice 3 addition -----------------------------------------------
    description: str | None = _SENTINEL,
    # Slice 4 additions ----------------------------------------------
    risk_profile_json: str | None = _SENTINEL,
    automation_tier_achievable: int | None = _SENTINEL,
    last_actor: str | None = _SENTINEL,
) -> dict[str, Any]:
    """Update metadata fields for a task. Only provided fields change.

    State changes are recorded in task_state_history with optional reason.

    Sentinel discipline: nullable text fields use _SENTINEL so callers
    can explicitly pass ``None`` to clear a value (vs. "not provided").
    Enum-validated fields use ``None`` for "not provided" since their
    valid values are non-None strings.
    """
    sets: list[str] = []
    params: list[Any] = []

    if state is not None:
        if state not in VALID_STATES:
            raise ValueError(f"Invalid state {state!r}")
        sets.append("state = ?")
        params.append(state)
        if state == "done":
            sets.append("completed_at = ?")
            params.append(_now_iso())

    if urgency is not None:
        if urgency not in VALID_URGENCIES:
            raise ValueError(f"Invalid urgency {urgency!r}")
        sets.append("urgency = ?")
        params.append(urgency)

    if complexity is not _SENTINEL:
        sets.append("complexity = ?")
        params.append(complexity)

    if contract is not _SENTINEL:
        sets.append("contract = ?")
        params.append(contract)

    if snooze_until is not _SENTINEL:
        sets.append("snooze_until = ?")
        params.append(snooze_until)

    if note_uuid is not _SENTINEL:
        sets.append("note_uuid = ?")
        params.append(note_uuid)

    # Slice 2 fields -------------------------------------------------
    if task_kind is not None:
        if task_kind not in VALID_TASK_KINDS:
            raise ValueError(f"Invalid task_kind {task_kind!r}")
        sets.append("task_kind = ?")
        params.append(task_kind)

    if density is not None:
        if density not in VALID_DENSITIES:
            raise ValueError(f"Invalid density {density!r}")
        sets.append("density = ?")
        params.append(density)

    if outcome_text is not _SENTINEL:
        sets.append("outcome_text = ?")
        params.append(outcome_text)

    if next_action_text is not _SENTINEL:
        sets.append("next_action_text = ?")
        params.append(next_action_text)

    if definition_of_done is not _SENTINEL:
        sets.append("definition_of_done = ?")
        params.append(definition_of_done)

    if creation_effort is not None:
        if creation_effort not in VALID_CREATION_EFFORTS:
            raise ValueError(f"Invalid creation_effort {creation_effort!r}")
        sets.append("creation_effort = ?")
        params.append(creation_effort)

    if user_involvement is not None:
        if user_involvement not in VALID_USER_INVOLVEMENTS:
            raise ValueError(f"Invalid user_involvement {user_involvement!r}")
        sets.append("user_involvement = ?")
        params.append(user_involvement)

    if creation_provenance is not None:
        sets.append("creation_provenance = ?")
        params.append(creation_provenance)

    if has_deadline is not None:
        sets.append("has_deadline = ?")
        params.append(int(bool(has_deadline)))

    if deadline_date is not _SENTINEL:
        sets.append("deadline_date = ?")
        params.append(deadline_date)

    if has_dependency is not None:
        sets.append("has_dependency = ?")
        params.append(int(bool(has_dependency)))

    if dependency_hint is not _SENTINEL:
        sets.append("dependency_hint = ?")
        params.append(dependency_hint)

    if description is not _SENTINEL:
        sets.append("description = ?")
        params.append(description)

    # Slice 4 fields -------------------------------------------------
    if risk_profile_json is not _SENTINEL:
        sets.append("risk_profile_json = ?")
        params.append(risk_profile_json)

    if automation_tier_achievable is not _SENTINEL:
        sets.append("automation_tier_achievable = ?")
        params.append(automation_tier_achievable)

    if last_actor is not _SENTINEL:
        if last_actor is not None and last_actor not in {"agent", "user"}:
            raise ValueError(
                f"Invalid last_actor {last_actor!r}: expected 'agent', 'user', or None"
            )
        sets.append("last_actor = ?")
        params.append(last_actor)

    if not sets:
        return {"task_id": task_id, "changed": False}

    sets.append("updated_at = ?")
    params.append(_now_iso())
    params.append(task_id)

    conn = get_connection()
    try:
        # Record state change history
        if state is not None:
            old_row = conn.execute(
                "SELECT state FROM task_metadata WHERE task_id = ?", (task_id,)
            ).fetchone()
            old_state = old_row["state"] if old_row else None

            if old_state != state:
                conn.execute(
                    """INSERT INTO task_state_history
                       (task_id, old_state, new_state, changed_at, reason)
                       VALUES (?, ?, ?, ?, ?)""",
                    (task_id, old_state, state, _now_iso(), reason),
                )

        conn.execute(
            f"UPDATE task_metadata SET {', '.join(sets)} WHERE task_id = ?",
            params,
        )
        conn.commit()
    finally:
        conn.close()

    logger.info("Task metadata updated: %s", task_id)
    return {"task_id": task_id, "changed": True}


def delete(task_id: str) -> bool:
    """Delete a task's metadata and state history. Returns True if found.

    Writes a tombstone row to ``task_state_history`` (new_state='deleted')
    before removing the metadata, so the deletion is visible in timelines.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT state FROM task_metadata WHERE task_id = ?", (task_id,)
        ).fetchone()
        if not row:
            return False
        # Tombstone: record deletion in history before removing data
        now = _now_iso()
        conn.execute(
            """INSERT INTO task_state_history
               (task_id, old_state, new_state, changed_at, reason)
               VALUES (?, ?, 'deleted', ?, 'deleted')""",
            (task_id, row["state"], now),
        )
        conn.execute("DELETE FROM task_sessions WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM task_metadata WHERE task_id = ?", (task_id,))
        conn.commit()
        logger.info("Task metadata deleted (tombstone written): %s", task_id)
        return True
    finally:
        conn.close()


def query(
    state: str | None = None,
    urgency: str | None = None,
    contract: str | None = None,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    """Query task metadata with optional filters."""
    clauses: list[str] = []
    params: list[Any] = []

    if state is not None:
        clauses.append("state = ?")
        params.append(state)
    if urgency is not None:
        clauses.append("urgency = ?")
        params.append(urgency)
    if contract is not None:
        clauses.append("contract = ?")
        params.append(contract)
    if not include_archived:
        clauses.append("archived_at IS NULL")

    where = " AND ".join(clauses) if clauses else "1=1"

    conn = get_connection()
    try:
        rows = conn.execute(
            f"SELECT * FROM task_metadata WHERE {where} ORDER BY updated_at DESC",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def search_by_description(
    query_text: str,
    *,
    limit: int = 50,
    include_archived: bool = False,
    include_done: bool = True,
) -> list[dict[str, Any]]:
    """Case-insensitive substring search over the description column.

    Returns task records whose ``description`` contains ``query_text``
    (LIKE '%...%' with SQL LIKE escaping). Ordered by most recently
    updated first.

    Args:
        query_text: Substring to search for. Empty string returns nothing
            (we don't want a "search for nothing" call to return the whole
            store).
        limit: Maximum results (default 50). The store is small enough
            that LIKE without an index is microseconds; the limit is
            mainly for consumers to keep result sizes manageable.
        include_archived: Include rows with ``archived_at`` set.
        include_done: Include rows with ``state='done'``.

    NULL descriptions (legacy rows not yet backfilled by ``task_sync``)
    are excluded automatically since SQLite ``LIKE`` against NULL is
    NULL/false.
    """
    if not query_text or not query_text.strip():
        return []

    # Escape LIKE wildcards so a query containing '%' or '_' doesn't
    # silently broaden the match. Standard LIKE-with-ESCAPE pattern.
    escaped = (
        query_text.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )

    clauses: list[str] = ["description IS NOT NULL"]
    params: list[Any] = []

    clauses.append("LOWER(description) LIKE LOWER(?) ESCAPE '\\'")
    params.append(f"%{escaped}%")

    if not include_archived:
        clauses.append("archived_at IS NULL")
    if not include_done:
        clauses.append("state != 'done'")

    where = " AND ".join(clauses)

    conn = get_connection()
    try:
        rows = conn.execute(
            f"""SELECT * FROM task_metadata WHERE {where}
                ORDER BY updated_at DESC LIMIT ?""",
            params + [int(limit)],
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_history(task_id: str) -> list[dict[str, Any]]:
    """Get state change history for a task, newest first."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT * FROM task_state_history
               WHERE task_id = ? ORDER BY changed_at DESC""",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_events_in_range(since: str, until: str) -> list[dict[str, Any]]:
    """Get all task state changes within a time range.

    Args:
        since: ISO datetime string (inclusive lower bound).
        until: ISO datetime string (exclusive upper bound).

    Returns:
        List of dicts with task_id, old_state, new_state, changed_at, reason.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT h.task_id, h.old_state, h.new_state, h.changed_at,
                      h.reason
               FROM task_state_history h
               WHERE h.changed_at >= ? AND h.changed_at < ?
               ORDER BY h.changed_at ASC""",
            (since, until),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def counts_by_state() -> dict[str, int]:
    """Get task counts grouped by state (excluding archived)."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT state, COUNT(*) as count FROM task_metadata
               WHERE archived_at IS NULL GROUP BY state"""
        ).fetchall()
        return {r["state"]: r["count"] for r in rows}
    finally:
        conn.close()


def mark_archived(task_id: str) -> None:
    """Mark a task as archived (sets archived_at timestamp).

    Also writes a history row so archival is visible in timelines.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT state FROM task_metadata WHERE task_id = ?", (task_id,)
        ).fetchone()
        now = _now_iso()
        conn.execute(
            "UPDATE task_metadata SET archived_at = ?, updated_at = ? WHERE task_id = ?",
            (now, now, task_id),
        )
        if row:
            conn.execute(
                """INSERT INTO task_state_history
                   (task_id, old_state, new_state, changed_at, reason)
                   VALUES (?, ?, 'archived', ?, 'archived')""",
                (task_id, row["state"], now),
            )
        conn.commit()
    finally:
        conn.close()


# ── Session assignment ─────────────────────────────────────────


def assign_session(task_id: str, session_id: str) -> dict[str, Any]:
    """Record a session as working on a task. Idempotent (INSERT OR IGNORE)."""
    now = _now_iso()
    conn = get_connection()
    try:
        conn.execute(
            """INSERT OR IGNORE INTO task_sessions
               (task_id, session_id, assigned_at)
               VALUES (?, ?, ?)""",
            (task_id, session_id, now),
        )
        conn.commit()
    finally:
        conn.close()

    logger.info("Session %s assigned to task %s", session_id[:8], task_id)
    return {"task_id": task_id, "session_id": session_id, "assigned_at": now}


def get_sessions(task_id: str) -> list[dict[str, Any]]:
    """Get all sessions assigned to a task, ordered by assignment time."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT task_id, session_id, assigned_at
               FROM task_sessions
               WHERE task_id = ? ORDER BY assigned_at""",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── Tag cache (mirrors markdown tags from task lines) ──────────
#
# The markdown task line is the source of truth for tags. This table is a
# cache rebuilt by task_sync on each run. Do not treat it as authoritative —
# if it disagrees with the line, the line wins.


def set_task_tags(
    task_id: str,
    tags: list[tuple[str, bool]],
) -> None:
    """Replace all tag rows for a task with the given list.

    Args:
        task_id: The task this tag set applies to.
        tags: Iterable of (tag, is_namespace) pairs. Tag strings must NOT
              include the leading '#'.
    """
    conn = get_connection()
    try:
        conn.execute("DELETE FROM task_tags WHERE task_id = ?", (task_id,))
        if tags:
            conn.executemany(
                """INSERT OR REPLACE INTO task_tags
                   (task_id, tag, is_namespace) VALUES (?, ?, ?)""",
                [(task_id, tag, 1 if is_ns else 0) for tag, is_ns in tags],
            )
        conn.commit()
    finally:
        conn.close()


def get_task_tags(task_id: str) -> list[dict[str, Any]]:
    """Return all tag rows for a task."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT task_id, tag, is_namespace FROM task_tags WHERE task_id = ? ORDER BY tag",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def tasks_with_tag(
    tag: str,
    *,
    prefix_match: bool = False,
    namespace_only: bool = False,
) -> list[str]:
    """Return task IDs whose tag cache contains ``tag``.

    With ``prefix_match=True``, also matches descendant tags (e.g. a query
    for ``"paper"`` returns tasks tagged ``paper``, ``paper/ecg``, and
    ``paper/ecg/experiments``). Only non-archived tasks are returned.
    """
    clauses = ["t.archived_at IS NULL"]
    params: list[Any] = []

    if prefix_match:
        clauses.append("(tt.tag = ? OR tt.tag LIKE ?)")
        params.extend([tag, f"{tag}/%"])
    else:
        clauses.append("tt.tag = ?")
        params.append(tag)

    if namespace_only:
        clauses.append("tt.is_namespace = 1")

    where = " AND ".join(clauses)

    conn = get_connection()
    try:
        rows = conn.execute(
            f"""SELECT DISTINCT tt.task_id FROM task_tags tt
                JOIN task_metadata t ON t.task_id = tt.task_id
                WHERE {where}
                ORDER BY tt.task_id""",
            params,
        ).fetchall()
        return [r["task_id"] for r in rows]
    finally:
        conn.close()


def distinct_namespace_tags(recent_days: int = 14) -> list[dict[str, Any]]:
    """Return the full set of namespacey tags with open-task counts.

    Result: ``[{"tag": "paper/ecg-classifier", "count": 4, "recent_count": 2}, ...]``
    ordered by tag ascending. Only counts non-archived tasks.

    ``recent_count`` counts tasks whose ``created_at`` falls within the
    last ``recent_days`` days. Callers can use this to build a relevance
    score (e.g. ``count + 2 * recent_count``) for UI ranking.
    """
    # Compute the cutoff in application code so the SQL stays portable.
    from datetime import datetime, timedelta, timezone
    days = max(0, int(recent_days or 0))
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT tt.tag AS tag,
                      COUNT(DISTINCT tt.task_id) AS count,
                      SUM(CASE WHEN t.created_at >= ? THEN 1 ELSE 0 END) AS recent_count
               FROM task_tags tt
               JOIN task_metadata t ON t.task_id = tt.task_id
               WHERE tt.is_namespace = 1 AND t.archived_at IS NULL
               GROUP BY tt.tag
               ORDER BY tt.tag""",
            (cutoff_iso,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            # SUM(CASE ...) returns int, but guard against None on empty aggregates.
            d["recent_count"] = int(d.get("recent_count") or 0)
            out.append(d)
        return out
    finally:
        conn.close()
