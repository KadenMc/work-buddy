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

# ───────────────────────────────────────────────────────────────────
# RETIRED. The ``_SCHEMA`` string and ``_SLICE_N_COLUMNS`` lists below
# are no longer executed at runtime — schema management has moved to
# the versioned migration ladder in
# ``work_buddy/obsidian/tasks/migrations.py``. These constants are
# preserved here as documentation of the schema's historical shape;
# delete them once the project is comfortable that the migration
# ladder is the unambiguous source of truth.
# ───────────────────────────────────────────────────────────────────
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
    -- GTD vocabulary -----------------------------------------------
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
    -- Description text -------------------------------------------
    -- Human-readable task text extracted from the master-list line
    -- (checkbox / tags / wikilink / plugin emojis / 🆔 stripped).
    -- NULL on legacy rows; backfilled by task_sync from the file.
    -- Source of truth: the markdown line. Store follows file (same
    -- precedent as the checkbox / note_uuid reconciliation paths).
    description     TEXT,
    -- Risk model + automation tiers + last-actor ------------------
    -- ``risk_profile_json``: JSON blob with the four dimensions
    -- (financial, privacy, accuracy, compute) + three amplifiers
    -- (reversibility, regret_potential, inference_uncertainty).
    -- NULL = "not yet classified" — the resolver treats NULL as the
    -- safe-profile fallback (low across the board, low amplifiers).
    -- Populated by the Clarify prompt at task-proposal time.
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
    -- legacy tasks created before the risk-model columns landed.
    last_actor      TEXT,
    -- Action-context resolution layer ----------------------------
    -- ``agent_required_contexts`` / ``user_required_contexts``: JSON
    -- arrays of context tokens (e.g. ``@filesystem``, ``@email_send``)
    -- the agent / user must each be in for this action to fire.  Read
    -- by ``work_buddy.automation.contexts.resolve_who_can_act`` against
    -- the live tool-status cache to decide who-can-act-now (lazy,
    -- never stored).  NULL on legacy rows (treated as empty list = no
    -- constraints).
    agent_required_contexts TEXT,
    user_required_contexts  TEXT,
    -- ``required_contexts_source``: 'agent_inferred' | 'user_authored'
    -- | NULL.  Provenance for the two lists above — flips to
    -- ``user_authored`` the first time the user edits the inferred
    -- set so future re-runs of Clarify don't clobber the user's
    -- ownership.
    required_contexts_source TEXT,
    -- Per-action-item pointer -------------------------------------
    -- ``current_action_item_id``: foreign key to task_action_items.id
    -- naming the step the user is currently focused on.  NULL for
    -- tasks that haven't been developed (sparse tasks at capture time).
    -- The master-list view renders this item's description as the
    -- "current step" badge, and the engage view resolves tier +
    -- contexts against this item's profile rather than the parent
    -- task's.  Set/cleared by the develop-at-pickup flow + the
    -- action_items CRUD.
    current_action_item_id INTEGER
);

CREATE TABLE IF NOT EXISTS task_action_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL,
    sequence        INTEGER NOT NULL,
    description     TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'pending',
                    -- 'pending' | 'in_progress' | 'done' | 'skipped'
    risk_profile_json TEXT,
    agent_required_contexts TEXT,   -- JSON array, mirrors task_metadata
    user_required_contexts TEXT,
    definition_of_done TEXT,
    authorship      TEXT NOT NULL DEFAULT 'agent_unapproved',
                    -- 'user' | 'agent_approved' | 'agent_unapproved'
    completed_at    TEXT,
    handoff_package_path TEXT,       -- vault path to the prep package
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    deleted_at      TEXT,            -- soft-delete; NULL = live row
    FOREIGN KEY (task_id) REFERENCES task_metadata(task_id),
                    -- FK has NO ON DELETE action: parent soft-deletes only;
                    -- raw DELETE on task_metadata is rejected by SQLite.
    UNIQUE(task_id, sequence)
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

-- Single-row table recording the last successful task_sync run. The
-- dashboard reads ``last_full_sync_at`` to render the "synced Xm ago"
-- freshness label so the user can tell at a glance how stale the
-- SQLite-primary view is relative to the markdown source. The CHECK
-- constraint enforces single-row semantics — every write upserts row 1.
CREATE TABLE IF NOT EXISTS task_sync_status (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    last_full_sync_at   TEXT,                                -- ISO UTC
    last_sync_created   INTEGER NOT NULL DEFAULT 0,
    last_sync_updated   INTEGER NOT NULL DEFAULT 0,
    last_sync_deleted   INTEGER NOT NULL DEFAULT 0,
    updated_at          TEXT NOT NULL
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
CREATE INDEX IF NOT EXISTS idx_action_items_task
    ON task_action_items(task_id, sequence);
CREATE INDEX IF NOT EXISTS idx_action_items_state
    ON task_action_items(state);
"""

VALID_STATES = {"inbox", "mit", "focused", "snoozed", "done"}
VALID_URGENCIES = {"low", "medium", "high"}
VALID_COMPLEXITIES = {"simple", "moderate", "complex", None}

# GTD-vocabulary enums --------------------------------------------------
# task_kind enum. 'periodic' and 'habit' ship as forward-compat values
# (a future reminder system will drive them); 'task' is the default.
VALID_TASK_KINDS = {"task", "periodic", "habit"}

# density enum. 'dense' is forward-compat; not used today.
VALID_DENSITIES = {"sparse", "developed", "dense"}

# creation_effort: how informed was the agent that wrote this task?
VALID_CREATION_EFFORTS = {"sparse", "medium", "developed"}

# user_involvement: was the user actively engaged or did the agent infer it?
VALID_USER_INVOLVEMENTS = {"low", "medium", "high"}

# creation_provenance is intentionally OPEN (no validator) — new sources
# (telegram, calendar, smart-source, …) get to register their own
# provenance string without a code change. Convention: 'manual' or
# 'agent_inferred_from_*'.

# Risk-model enums ------------------------------------------------------
# last_actor: who most recently acted on the task. NULL = legacy (no
# actor recorded yet). The set is closed because the resolver branches
# on it; per-action-item actors would need a separate column.
VALID_LAST_ACTORS = {"agent", "user", None}

# Action-context enums --------------------------------------------------
# required_contexts_source: provenance of the agent / user context lists.
# NULL = legacy (no contexts classified yet). 'agent_inferred' = Clarify
# populated; 'user_authored' = the user edited the inferred set (locks
# future Clarify runs from clobbering it).
VALID_CONTEXT_SOURCES = {"agent_inferred", "user_authored", None}

# Column descriptors retained for test back-compat. The MigrationRunner
# now owns schema evolution (see ``architecture/migrations``); these
# lists are reference material and validation surface for legacy tests
# that import them. Format:
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

# Description-column descriptor. Nullable; backfilled by task_sync from
# the markdown master task list (markdown is the source of truth).
_SLICE_3_COLUMNS: list[tuple[str, str, str | None, bool]] = [
    ("description", "TEXT", None, False),
]

# Risk model + automation-tier cache + last-actor column descriptors.
# All nullable. ``risk_profile_json`` is a JSON blob of the four
# dimensions + three amplifiers — see ``work_buddy.automation.risk``.
# ``automation_tier_achievable`` caches a pure function of the task;
# rebuilt by Clarify on creation, NULL until then (resolver re-derives
# lazily). ``last_actor`` is detected at mutation time via
# ``consent.get_consent_context_info()`` — legacy rows leave it NULL.
_SLICE_4_COLUMNS: list[tuple[str, str, str | None, bool]] = [
    ("risk_profile_json", "TEXT", None, False),
    ("automation_tier_achievable", "INTEGER", None, False),
    ("last_actor", "TEXT", None, False),
]

# Action-context resolution column descriptors. All nullable JSON /
# enum columns. ``agent_required_contexts`` and ``user_required_contexts``
# are TEXT JSON arrays. Empty / NULL means "no required contexts" (legacy
# rows). ``required_contexts_source`` carries provenance ('agent_inferred'
# | 'user_authored' | NULL); flips to ``user_authored`` once a human
# edits the inferred set so future Clarify re-runs don't clobber the edit.
_SLICE_5A_COLUMNS: list[tuple[str, str, str | None, bool]] = [
    ("agent_required_contexts", "TEXT", None, False),
    ("user_required_contexts", "TEXT", None, False),
    ("required_contexts_source", "TEXT", None, False),
]

# Per-action-item pointer column descriptor. ``current_action_item_id``
# is a foreign key into ``task_action_items.id`` — nullable because
# most legacy tasks don't have action items.
_SLICE_7_COLUMNS: list[tuple[str, str, str | None, bool]] = [
    ("current_action_item_id", "INTEGER", None, False),
]

# All migration-added column lists, in schema-evolution order. Retained
# for test back-compat; the live migration path is in the MigrationRunner.
_ALL_MIGRATED_COLUMNS: list[tuple[str, str, str | None, bool]] = (
    _SLICE_2_COLUMNS + _SLICE_3_COLUMNS + _SLICE_4_COLUMNS
    + _SLICE_5A_COLUMNS + _SLICE_7_COLUMNS
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

    Schema is brought up to date by the versioned migration ladder
    in ``work_buddy.obsidian.tasks.migrations`` — see that module for
    the full ladder (m001..m007 currently) and ``work_buddy.storage.migrations``
    for the runner's safety invariants (transaction wrapping, PRAGMA
    foreign_keys discipline, race lock, downgrade guard, hash audit).
    The runner is cheap when already at latest (one PRAGMA read + one
    hash-verify pass; no DDL).
    """
    path = _db_path()
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _migrate_schema(conn)
    return conn


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Apply any pending migrations to bring the DB to current schema.

    Delegates to ``TASK_MIGRATIONS.run(conn)``. The schema history
    lives in ``work_buddy/obsidian/tasks/migrations.py`` as a numbered
    ladder of idempotent DDL callables. New schema work appends a new
    migration step (e.g. m010) — never modifies existing ones (the
    runner's hash audit refuses edits to shipped migrations).
    """
    # Lazy import: keeps the module load cycle clean if migrations.py
    # ever needs to reference store.py for legitimate reasons.
    from work_buddy.obsidian.tasks.migrations import TASK_MIGRATIONS
    TASK_MIGRATIONS.run(conn)


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
    # GTD vocabulary -------------------------------------------------
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
    # Description ----------------------------------------------------
    description: str | None = None,
    # Risk model + automation tier + actor ---------------------------
    risk_profile_json: str | None = None,
    automation_tier_achievable: int | None = None,
    last_actor: str | None = None,
    # Action-context resolution --------------------------------------
    agent_required_contexts: str | None = None,
    user_required_contexts: str | None = None,
    required_contexts_source: str | None = None,
) -> dict[str, Any]:
    """Create a metadata record for a new task.

    Called when create_task() generates a new 🆔.

    GTD-vocabulary fields default to the "legacy task" assumption:
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
    if required_contexts_source not in VALID_CONTEXT_SOURCES:
        raise ValueError(
            f"Invalid required_contexts_source {required_contexts_source!r}: "
            f"expected 'agent_inferred', 'user_authored', or None"
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
                risk_profile_json, automation_tier_achievable, last_actor,
                agent_required_contexts, user_required_contexts,
                required_contexts_source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?,
                       ?, ?, ?,
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
                agent_required_contexts, user_required_contexts,
                required_contexts_source,
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


def get(task_id: str, *, include_deleted: bool = False) -> dict[str, Any] | None:
    """Get metadata for a task by ID. Returns None if not found.

    Soft-deleted rows (``deleted_at IS NOT NULL``) are invisible by
    default. Pass ``include_deleted=True`` for recovery contexts that
    need to inspect tombstoned rows (e.g. the snapshot-restore
    validation path in ``architecture/backups``).
    """
    conn = get_connection()
    try:
        if include_deleted:
            row = conn.execute(
                "SELECT * FROM task_metadata WHERE task_id = ?", (task_id,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM task_metadata "
                "WHERE task_id = ? AND deleted_at IS NULL",
                (task_id,),
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
    # Description ----------------------------------------------------
    description: str | None = _SENTINEL,
    # task_sync emoji-drift addition ---------------------------------
    # Explicit completed_at writes — normally stamped automatically
    # when ``state`` transitions to ``done``, but ``task_sync`` needs
    # to backfill the date the user wrote next to ``✅`` in the
    # markdown rather than the moment sync happened to run. Sentinel
    # so callers can pass ``None`` to clear.
    completed_at: str | None = _SENTINEL,
    # Risk model + automation tier + actor ---------------------------
    risk_profile_json: str | None = _SENTINEL,
    automation_tier_achievable: int | None = _SENTINEL,
    last_actor: str | None = _SENTINEL,
    # Action-context resolution --------------------------------------
    agent_required_contexts: str | None = _SENTINEL,
    user_required_contexts: str | None = _SENTINEL,
    required_contexts_source: str | None = _SENTINEL,
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

    # GTD-vocabulary fields ------------------------------------------
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

    if completed_at is not _SENTINEL:
        # Explicit write wins over the state→done auto-stamp. If the
        # caller passes both ``state="done"`` and an explicit
        # ``completed_at``, the explicit value is what lands (later
        # append overrides earlier in the same UPDATE — both columns
        # end up in ``sets``, and SQLite uses the last assignment
        # for the column).
        sets.append("completed_at = ?")
        params.append(completed_at)

    # Risk-model fields ----------------------------------------------
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

    # Action-context fields ------------------------------------------
    if agent_required_contexts is not _SENTINEL:
        sets.append("agent_required_contexts = ?")
        params.append(agent_required_contexts)

    if user_required_contexts is not _SENTINEL:
        sets.append("user_required_contexts = ?")
        params.append(user_required_contexts)

    if required_contexts_source is not _SENTINEL:
        if required_contexts_source not in VALID_CONTEXT_SOURCES:
            raise ValueError(
                f"Invalid required_contexts_source {required_contexts_source!r}: "
                f"expected 'agent_inferred', 'user_authored', or None"
            )
        sets.append("required_contexts_source = ?")
        params.append(required_contexts_source)

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
    """Soft-delete a task. Returns True if the row existed and was flipped.

    The only way to remove a task_metadata row is to set its
    ``deleted_at`` timestamp. Query paths default-filter
    ``WHERE deleted_at IS NULL`` so the row becomes invisible to
    normal reads, but the data — including referenced ``task_action_items``
    and ``task_tags`` — is preserved forever. Use ``restore(task_id)``
    to bring it back.

    A row is added to ``task_state_history`` with ``new_state='deleted'``
    so the timeline shows the lifecycle event. The row's prior
    ``state`` is preserved on ``task_metadata`` itself (we don't blank
    it) so a future ``restore()`` can return the task to its previous
    live state.

    Calling on an already-soft-deleted row is a no-op (returns False).
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT state, deleted_at FROM task_metadata WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if not row:
            return False
        if row["deleted_at"] is not None:
            # Already soft-deleted; idempotent no-op.
            return False
        now = _now_iso()
        conn.execute(
            """INSERT INTO task_state_history
               (task_id, old_state, new_state, changed_at, reason)
               VALUES (?, ?, 'deleted', ?, 'deleted')""",
            (task_id, row["state"], now),
        )
        conn.execute(
            "UPDATE task_metadata SET deleted_at = ?, updated_at = ? "
            "WHERE task_id = ?",
            (now, now, task_id),
        )
        conn.commit()
        logger.info("Task metadata soft-deleted: %s", task_id)
        return True
    finally:
        conn.close()


def restore(task_id: str) -> bool:
    """Clear the ``deleted_at`` flag on a soft-deleted task.

    Returns True if a row existed and had ``deleted_at`` set. The
    inverse of :func:`delete`. Records a ``new_state='restored'`` row
    in ``task_state_history`` so the lifecycle is audit-visible.

    Used by the snapshot-restore path (see ``architecture/backups``)
    and by any future "undo deletion" UX (e.g. a ``task_recover``
    capability).
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT state, deleted_at FROM task_metadata WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if not row or row["deleted_at"] is None:
            return False
        now = _now_iso()
        conn.execute(
            """INSERT INTO task_state_history
               (task_id, old_state, new_state, changed_at, reason)
               VALUES (?, 'deleted', ?, ?, 'restored')""",
            (task_id, row["state"], now),
        )
        conn.execute(
            "UPDATE task_metadata SET deleted_at = NULL, updated_at = ? "
            "WHERE task_id = ?",
            (now, task_id),
        )
        conn.commit()
        logger.info("Task metadata restored: %s", task_id)
        return True
    finally:
        conn.close()


def query(
    state: str | None = None,
    urgency: str | None = None,
    contract: str | None = None,
    include_archived: bool = False,
    include_deleted: bool = False,
) -> list[dict[str, Any]]:
    """Query task metadata with optional filters.

    Soft-deleted rows (``deleted_at IS NOT NULL``) are excluded by
    default. Pass ``include_deleted=True`` for recovery / audit contexts
    that need to see them.
    """
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
    if not include_deleted:
        clauses.append("deleted_at IS NULL")

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
    include_deleted: bool = False,
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
    if not include_deleted:
        clauses.append("deleted_at IS NULL")

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
    """Get task counts grouped by state (excluding archived AND soft-deleted)."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT state, COUNT(*) as count FROM task_metadata
               WHERE archived_at IS NULL AND deleted_at IS NULL
               GROUP BY state"""
        ).fetchall()
        return {r["state"]: r["count"] for r in rows}
    finally:
        conn.close()


def get_sync_status() -> dict[str, Any] | None:
    """Return the last-recorded task_sync run metadata, or None if never run.

    Used by the dashboard's "synced Xm ago" freshness label. The row
    contains ``last_full_sync_at`` (ISO UTC), counts of created /
    updated / deleted rows from the most recent sync, and ``updated_at``
    (when the row itself was last written, which equals the sync
    completion time).
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT last_full_sync_at, last_sync_created, last_sync_updated, "
            "last_sync_deleted, updated_at FROM task_sync_status WHERE id = 1"
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def set_sync_status(
    created: int = 0,
    updated: int = 0,
    deleted: int = 0,
) -> None:
    """Upsert the task_sync_status row with the current run's counts.

    Always writes to row 1 (single-row table enforced by the CHECK
    constraint). Called by ``task_sync`` at the end of a successful
    reconciliation pass; the timestamp is generated server-side so the
    caller never has to format it.
    """
    now = _now_iso()
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO task_sync_status
                 (id, last_full_sync_at, last_sync_created,
                  last_sync_updated, last_sync_deleted, updated_at)
               VALUES (1, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 last_full_sync_at = excluded.last_full_sync_at,
                 last_sync_created = excluded.last_sync_created,
                 last_sync_updated = excluded.last_sync_updated,
                 last_sync_deleted = excluded.last_sync_deleted,
                 updated_at        = excluded.updated_at""",
            (now, int(created), int(updated), int(deleted), now),
        )
        conn.commit()
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
    """Reconcile a task's tag set against a target via diff-and-update.

    This function NEVER deletes — it computes the diff between the
    currently-cached tag set and the target, then INSERTs rows that
    are new and UPDATEs ``is_namespace`` where the classification has
    changed. Rows present in the cache but absent from the target are
    LEFT IN PLACE.

    The trade-off: stale tag rows can accumulate if the user removes
    a tag from a task line in markdown. Acceptable because (a)
    accumulation is bounded by total tag-vocabulary churn, which is
    slow; (b) the alternative is a hard DELETE, which is exactly the
    wide-fanout-delete vector class the soft-delete + cascade-drop
    discipline (see ``architecture/backups``, ``tasks/task_delete``)
    closes off. If stale-tag accumulation becomes a real problem, a
    future migration can add ``deleted_at`` to ``task_tags`` and move
    this function to true soft-delete semantics.

    Args:
        task_id: The task this tag set applies to.
        tags: Iterable of (tag, is_namespace) pairs. Tag strings must
            NOT include the leading '#'.
    """
    target: dict[str, int] = {
        tag: (1 if is_ns else 0) for tag, is_ns in tags
    }
    conn = get_connection()
    try:
        existing: dict[str, int] = {
            row["tag"]: row["is_namespace"]
            for row in conn.execute(
                "SELECT tag, is_namespace FROM task_tags WHERE task_id = ?",
                (task_id,),
            )
        }
        # INSERT rows that are new (in target, not in existing).
        new_rows = [
            (task_id, tag, is_ns)
            for tag, is_ns in target.items()
            if tag not in existing
        ]
        if new_rows:
            conn.executemany(
                "INSERT INTO task_tags (task_id, tag, is_namespace) "
                "VALUES (?, ?, ?)",
                new_rows,
            )
        # UPDATE rows whose namespace classification has changed.
        for tag, is_ns in target.items():
            if tag in existing and existing[tag] != is_ns:
                conn.execute(
                    "UPDATE task_tags SET is_namespace = ? "
                    "WHERE task_id = ? AND tag = ?",
                    (is_ns, task_id, tag),
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
    ``paper/ecg/experiments``). Only non-archived, non-soft-deleted tasks
    are returned.
    """
    clauses = ["t.archived_at IS NULL", "t.deleted_at IS NULL"]
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
    ordered by tag ascending. Only counts non-archived, non-soft-deleted
    tasks.

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
               WHERE tt.is_namespace = 1
                 AND t.archived_at IS NULL
                 AND t.deleted_at IS NULL
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
