"""SQLite entity registry store.

Schema lives in :mod:`work_buddy.entities.migrations` (versioned via the
``PRAGMA user_version`` migration framework). This module is the CRUD
surface for every mutation and every read.

Identity:

- ``entities`` — one row per canonical entity. Surrogate integer ``id``
  is the stable identifier. ``canonical_norm`` is the case-folded form
  used for lookup and uniqueness.
- ``entity_tags`` — hierarchical, multi-valued. ``person/family``,
  ``place/work``. Filter is prefix-match: ``tag='person'`` matches
  ``person/family``.
- ``entity_aliases`` — alternative names. Globally unique
  ``alias_norm`` (an alias belongs to exactly one entity).

History:

- ``entity_references`` — append-only mention log. One row per
  observation. The reference recorder de-duplicates within a configurable
  window per ``(entity_id, source_path, source_kind)`` triple to keep
  the table useful as a signal rather than agent log spam.

Federated resolution is implemented one layer up (in the MCP wrappers
at :mod:`work_buddy.mcp_server.context_wrappers`). This module only
sees entity-store rows; the project registry is queried as a parallel
resolution source by the wrapper.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from work_buddy.config import load_config
from work_buddy.entities.migrations import ENTITY_MIGRATIONS
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


# ─── Configuration ──────────────────────────────────────────────────


VALID_AUTHORS: set[str] = {"user", "agent"}

# Source kinds the reference index accepts. Free-form enough to grow
# without a schema change; constrained enough that consumers can switch
# on the value confidently.
VALID_SOURCE_KINDS: set[str] = {
    "document",   # vault notes, repo files, anything path-shaped
    "chat",       # claude code / agent session
    "task",       # a task note or master list line
    "agent",      # autonomous agent action without a doc anchor
    "manual",     # explicit user-recorded reference
}

# De-dup window for reference recording. Same
# (entity_id, source_path, source_kind) within this many seconds is
# treated as one observation. One hour is a deliberate middle-ground:
# tight enough that a multi-day project file genuinely produces a new
# row each day, loose enough that an agent resolving the same name 50
# times during one session doesn't carpet the table.
_REFERENCE_DEDUP_SECONDS: int = 3600


def _db_path() -> Path:
    """Resolve the entity registry database path from config.

    Supports an ``entities.db_path`` override in ``config.yaml`` /
    ``config.local.yaml`` for tests and isolated installs. Mirrors the
    convention used by :mod:`work_buddy.projects.store`.
    """
    cfg = load_config()
    custom = cfg.get("entities", {}).get("db_path")
    if custom:
        from work_buddy.paths import repo_root
        p = Path(custom) if Path(custom).is_absolute() else repo_root() / custom
    else:
        from work_buddy.paths import resolve
        p = resolve("db/entities")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def get_connection() -> sqlite3.Connection:
    """Open (or create) the entity database with WAL mode + migrations."""
    path = _db_path()
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    ENTITY_MIGRATIONS.run(conn)
    return conn


# ─── Helpers ────────────────────────────────────────────────────────


def _now() -> str:
    """Millisecond-precision ISO 8601 UTC timestamp.

    Captured once per logical operation and reused across every INSERT
    in that operation's transaction — avoids the SQLite per-statement
    ``CURRENT_TIMESTAMP`` drift trap.
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _normalize_name(name: str) -> str:
    """Normalize a display name for case-insensitive lookup.

    Lowercase, trim leading/trailing whitespace, collapse runs of
    interior whitespace to a single space. Unlike project slugs we
    preserve spaces — entity names are display strings ("Max McKeen",
    "SickKids"), not URL components.
    """
    return " ".join(name.lower().strip().split())


def _normalize_tag(tag: str) -> str:
    """Normalize a hierarchical tag.

    Lowercase, trim, collapse adjacent slashes, strip leading/trailing
    slashes. ``person/family`` stays ``person/family``. ``Person//Family/``
    becomes ``person/family``. Whitespace around path segments is
    removed.
    """
    parts = [p.strip() for p in tag.lower().strip().strip("/").split("/")]
    return "/".join(p for p in parts if p)


def _publish_entity_event(event_type: str, payload: dict[str, Any]) -> None:
    """Best-effort publish to the dashboard event bus.

    Routes through ``publish_auto`` so callers from the dashboard
    publish in-process and callers from any other process route via
    the messaging-service bridge. Never raises — a missed event must
    not break an entity mutation.
    """
    try:
        from work_buddy.dashboard.events import publish_auto
        publish_auto(event_type, payload)
    except Exception:
        logger.exception("entities: event publish for %r failed", event_type)


# ─── Lookup ─────────────────────────────────────────────────────────


def resolve_name(name_or_alias: str) -> int | None:
    """Return the canonical ``entity_id`` for a name or alias, else None.

    Single entry point for any caller that has a string and needs to
    identify which entity it refers to. Resolution order:

    1. Exact canonical-name match (case-sensitive)
    2. Case-insensitive canonical-name match (via ``canonical_norm``)
    3. Alias match (via ``alias_norm``)

    A canonical match always wins over an alias match — if "Max" is
    canonical for one entity and an alias for another, the canonical
    owner is returned.
    """
    if not name_or_alias:
        return None
    norm = _normalize_name(name_or_alias)
    if not norm:
        return None
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM entities WHERE canonical_name = ?",
            (name_or_alias,),
        ).fetchone()
        if row:
            return row["id"]
        row = conn.execute(
            "SELECT id FROM entities WHERE canonical_norm = ?",
            (norm,),
        ).fetchone()
        if row:
            return row["id"]
        row = conn.execute(
            "SELECT entity_id FROM entity_aliases WHERE alias_norm = ?",
            (norm,),
        ).fetchone()
        if row:
            return row["entity_id"]
        return None
    finally:
        conn.close()


def get_entity(name_or_id: str | int) -> dict[str, Any] | None:
    """Return an entity record by id, canonical name, or alias.

    Result includes the entity row plus ``tags`` and ``aliases`` lists.
    Returns ``None`` if no match.
    """
    eid: int | None
    if isinstance(name_or_id, int):
        eid = name_or_id
    else:
        eid = resolve_name(name_or_id)
    if eid is None:
        return None
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM entities WHERE id = ?", (eid,)
        ).fetchone()
        if not row:
            return None
        return _row_with_children(conn, row)
    finally:
        conn.close()


def _row_with_children(
    conn: sqlite3.Connection, row: sqlite3.Row,
) -> dict[str, Any]:
    """Decorate an entities row with its tags and aliases."""
    result = dict(row)
    eid = row["id"]
    result["tags"] = [
        dict(r) for r in conn.execute(
            "SELECT tag, tag_norm FROM entity_tags WHERE entity_id = ? "
            "ORDER BY tag_norm",
            (eid,),
        )
    ]
    result["aliases"] = [
        dict(r) for r in conn.execute(
            "SELECT alias, alias_norm FROM entity_aliases WHERE entity_id = ? "
            "ORDER BY alias_norm",
            (eid,),
        )
    ]
    return result


def list_entities(
    *,
    tag: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """List entities ordered by most-recently-updated first.

    ``tag`` filter is hierarchical: ``tag='person'`` returns entities
    tagged ``person`` AND entities tagged ``person/family``,
    ``person/colleague``, etc. The match is on ``tag_norm`` with a
    prefix-and-slash expansion.

    ``limit`` caps the result set; ``None`` returns every match.
    """
    conn = get_connection()
    try:
        if tag is not None:
            norm = _normalize_tag(tag)
            if not norm:
                return []
            sql = (
                "SELECT e.* FROM entities e "
                "JOIN entity_tags t ON t.entity_id = e.id "
                "WHERE t.tag_norm = ? OR t.tag_norm LIKE ? "
                "GROUP BY e.id "
                "ORDER BY e.updated_at DESC"
            )
            params: tuple[Any, ...] = (norm, norm + "/%")
        else:
            sql = "SELECT * FROM entities ORDER BY updated_at DESC"
            params = ()
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        rows = conn.execute(sql, params).fetchall()
        return [_row_with_children(conn, r) for r in rows]
    finally:
        conn.close()


# ─── Mutation ───────────────────────────────────────────────────────


def create_entity(
    canonical_name: str,
    *,
    description: str | None = None,
    tags: Iterable[str] | None = None,
    aliases: Iterable[str] | None = None,
    author: str = "user",
) -> dict[str, Any]:
    """Create a new entity row with optional tags and aliases.

    Raises ``ValueError`` on duplicate canonical name (matched via
    ``canonical_norm``), invalid author, or alias collision with an
    existing canonical name or alias.

    Returns the freshly created entity record with tags and aliases
    populated.
    """
    if author not in VALID_AUTHORS:
        raise ValueError(
            f"Invalid author: {author!r}. Must be one of {VALID_AUTHORS}"
        )
    name = canonical_name.strip()
    if not name:
        raise ValueError("canonical_name cannot be empty or whitespace")
    norm = _normalize_name(name)

    now = _now()
    conn = get_connection()
    try:
        conn.execute("BEGIN")
        existing = conn.execute(
            "SELECT id, canonical_name FROM entities WHERE canonical_norm = ?",
            (norm,),
        ).fetchone()
        if existing:
            raise ValueError(
                f"Entity with canonical name {existing['canonical_name']!r} "
                f"already exists (id={existing['id']}). Use update_entity to "
                "modify it, or add this name as an alias."
            )

        cursor = conn.execute(
            "INSERT INTO entities "
            "(canonical_name, canonical_norm, description, author, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (name, norm, description, author, now, now),
        )
        entity_id = cursor.lastrowid
        assert entity_id is not None

        if tags is not None:
            _replace_tags_unchecked(conn, entity_id, tags)
        if aliases is not None:
            for alias in aliases:
                _add_alias_unchecked(conn, entity_id, alias)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    _publish_entity_event("entity.created", {
        "entity_id": entity_id, "canonical_name": name, "author": author,
    })
    result = get_entity(entity_id)
    assert result is not None
    return result


class _Sentinel:
    """Distinguishes 'not provided' from None in update kwargs."""

_NOT_SET = _Sentinel()


def update_entity(
    entity_id: int,
    *,
    canonical_name: str | _Sentinel = _NOT_SET,
    description: str | None | _Sentinel = _NOT_SET,
    author: str = "user",
) -> dict[str, Any] | None:
    """Update an entity's identity fields. Only provided fields change.

    ``canonical_name`` rename: re-normalizes and rejects if the new
    norm collides with another entity. ``description=None`` clears the
    description; omitting it leaves the description unchanged.

    Returns the updated record, or ``None`` if no such entity.
    """
    if author not in VALID_AUTHORS:
        raise ValueError(
            f"Invalid author: {author!r}. Must be one of {VALID_AUTHORS}"
        )
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if not row:
            return None

        now = _now()
        conn.execute("BEGIN")
        updates: list[str] = []
        params: list[Any] = []
        if not isinstance(canonical_name, _Sentinel):
            new_name = canonical_name.strip()
            if not new_name:
                raise ValueError(
                    "canonical_name cannot be empty or whitespace"
                )
            new_norm = _normalize_name(new_name)
            collision = conn.execute(
                "SELECT id FROM entities "
                "WHERE canonical_norm = ? AND id != ?",
                (new_norm, entity_id),
            ).fetchone()
            if collision:
                raise ValueError(
                    f"Rename collides with entity id={collision['id']} "
                    f"(canonical_norm={new_norm!r})"
                )
            updates.append("canonical_name = ?")
            params.append(new_name)
            updates.append("canonical_norm = ?")
            params.append(new_norm)
        if not isinstance(description, _Sentinel):
            updates.append("description = ?")
            params.append(description)

        if updates:
            updates.append("updated_at = ?")
            params.append(now)
            params.append(entity_id)
            conn.execute(
                f"UPDATE entities SET {', '.join(updates)} WHERE id = ?",
                params,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    _publish_entity_event("entity.updated", {
        "entity_id": entity_id, "author": author,
    })
    return get_entity(entity_id)


def delete_entity(entity_id: int, *, author: str = "user") -> bool:
    """Hard-delete an entity and cascade its tags, aliases, references.

    Returns ``True`` if a row was removed, ``False`` if no such entity.
    The cascade is what the migration's ``ON DELETE CASCADE`` clauses
    enforce — this function only issues the DELETE.

    The handoff design did not call for soft-delete on entities. If
    audit-after-delete becomes a need, add a new migration to flip
    this to soft-delete; for now hard-delete keeps the surface honest
    (a deleted entity is gone, not hidden).
    """
    if author not in VALID_AUTHORS:
        raise ValueError(
            f"Invalid author: {author!r}. Must be one of {VALID_AUTHORS}"
        )
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT canonical_name FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if not row:
            return False
        name = row["canonical_name"]
        conn.execute("BEGIN")
        conn.execute("DELETE FROM entities WHERE id = ?", (entity_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    _publish_entity_event("entity.deleted", {
        "entity_id": entity_id, "canonical_name": name, "author": author,
    })
    return True


# ─── Tags ───────────────────────────────────────────────────────────


def set_tags(
    entity_id: int, tags: Iterable[str], *, author: str = "user",
) -> dict[str, Any] | None:
    """Replace the full tag set on an entity.

    Tags are normalized via :func:`_normalize_tag`. Duplicates within
    the input collapse via the unique index. Empty tags after
    normalization are silently dropped.

    Returns the updated record, or ``None`` if no such entity.
    """
    if author not in VALID_AUTHORS:
        raise ValueError(
            f"Invalid author: {author!r}. Must be one of {VALID_AUTHORS}"
        )
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if not row:
            return None
        now = _now()
        conn.execute("BEGIN")
        _replace_tags_unchecked(conn, entity_id, tags)
        conn.execute(
            "UPDATE entities SET updated_at = ? WHERE id = ?",
            (now, entity_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    _publish_entity_event("entity.tags_changed", {
        "entity_id": entity_id, "author": author,
    })
    return get_entity(entity_id)


def _replace_tags_unchecked(
    conn: sqlite3.Connection,
    entity_id: int,
    tags: Iterable[str],
) -> None:
    """Replace an entity's tag set. Caller must hold the transaction."""
    conn.execute(
        "DELETE FROM entity_tags WHERE entity_id = ?", (entity_id,)
    )
    seen: set[str] = set()
    for tag in tags:
        display = tag.strip()
        norm = _normalize_tag(display)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        conn.execute(
            "INSERT INTO entity_tags (entity_id, tag, tag_norm) "
            "VALUES (?, ?, ?)",
            (entity_id, display, norm),
        )


# ─── Aliases ────────────────────────────────────────────────────────


def add_alias(
    entity_id: int, alias: str, *, author: str = "user",
) -> dict[str, Any] | None:
    """Add an alias to an entity.

    Raises ``ValueError`` if the alias collides with another entity's
    canonical name or another entity's alias. Idempotent for the same
    entity (re-adding the same alias is a no-op).

    Returns the updated record, or ``None`` if no such entity.
    """
    if author not in VALID_AUTHORS:
        raise ValueError(
            f"Invalid author: {author!r}. Must be one of {VALID_AUTHORS}"
        )
    display = alias.strip()
    if not display:
        raise ValueError("Empty alias not allowed")
    norm = _normalize_name(display)
    if not norm:
        raise ValueError(f"Alias {alias!r} normalizes to an empty string")

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if not row:
            return None
        clash_canonical = conn.execute(
            "SELECT id FROM entities "
            "WHERE canonical_norm = ? AND id != ?",
            (norm, entity_id),
        ).fetchone()
        if clash_canonical:
            raise ValueError(
                f"Alias {alias!r} collides with canonical name of "
                f"entity id={clash_canonical['id']}"
            )
        clash_alias = conn.execute(
            "SELECT entity_id FROM entity_aliases "
            "WHERE alias_norm = ? AND entity_id != ?",
            (norm, entity_id),
        ).fetchone()
        if clash_alias:
            raise ValueError(
                f"Alias {alias!r} already belongs to entity "
                f"id={clash_alias['entity_id']}"
            )
        conn.execute("BEGIN")
        _add_alias_unchecked(conn, entity_id, display)
        conn.execute(
            "UPDATE entities SET updated_at = ? WHERE id = ?",
            (_now(), entity_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    _publish_entity_event("entity.aliases_changed", {
        "entity_id": entity_id, "action": "add", "alias": display,
        "author": author,
    })
    return get_entity(entity_id)


def _add_alias_unchecked(
    conn: sqlite3.Connection, entity_id: int, alias: str,
) -> None:
    """Insert an alias. Caller validates collision and holds the txn."""
    display = alias.strip()
    if not display:
        return
    norm = _normalize_name(display)
    if not norm:
        return
    conn.execute(
        "INSERT OR IGNORE INTO entity_aliases "
        "(entity_id, alias, alias_norm) VALUES (?, ?, ?)",
        (entity_id, display, norm),
    )


def remove_alias(
    entity_id: int, alias: str, *, author: str = "user",
) -> dict[str, Any] | None:
    """Remove an alias by display string or normalized form.

    No-op if the alias isn't attached. Returns the updated record, or
    ``None`` if no such entity.
    """
    if author not in VALID_AUTHORS:
        raise ValueError(
            f"Invalid author: {author!r}. Must be one of {VALID_AUTHORS}"
        )
    norm = _normalize_name(alias)
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if not row:
            return None
        conn.execute("BEGIN")
        conn.execute(
            "DELETE FROM entity_aliases "
            "WHERE entity_id = ? AND alias_norm = ?",
            (entity_id, norm),
        )
        conn.execute(
            "UPDATE entities SET updated_at = ? WHERE id = ?",
            (_now(), entity_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    _publish_entity_event("entity.aliases_changed", {
        "entity_id": entity_id, "action": "remove", "alias": alias,
        "author": author,
    })
    return get_entity(entity_id)


# ─── References (append-only) ───────────────────────────────────────


def record_reference(
    entity_id: int,
    source_path: str,
    source_kind: str,
    *,
    snippet: str | None = None,
    occurred_at: str | None = None,
    dedup_window_seconds: int | None = None,
) -> int | None:
    """Append a reference row recording a mention of this entity.

    De-duplicates within ``dedup_window_seconds`` per
    ``(entity_id, source_path, source_kind)`` triple. Within the
    window, repeat calls return the existing row's ``id`` without
    inserting. Pass ``dedup_window_seconds=0`` to force an insert.

    Returns the reference ``id`` (new or existing), or ``None`` if the
    entity does not exist.

    ``source_kind`` must be one of :data:`VALID_SOURCE_KINDS`. Unknown
    kinds raise ``ValueError`` rather than being silently mapped — the
    set is deliberately small so adding a new one is a conscious
    decision documented at the call site.

    Append-only invariant: this function never deletes a reference.
    The de-dup is an INSERT-time gate, not a delete.
    """
    if source_kind not in VALID_SOURCE_KINDS:
        raise ValueError(
            f"Invalid source_kind: {source_kind!r}. Must be one of "
            f"{VALID_SOURCE_KINDS}"
        )
    if not source_path:
        raise ValueError("source_path cannot be empty")

    window = (
        _REFERENCE_DEDUP_SECONDS if dedup_window_seconds is None
        else int(dedup_window_seconds)
    )
    ts = occurred_at or _now()

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if not row:
            return None

        if window > 0:
            # ISO 8601 strings sort lexicographically when the format is
            # consistent (``isoformat(timespec='milliseconds')`` is). We
            # compute the cutoff as an ISO string by subtracting the
            # window from the current observation time.
            from datetime import timedelta
            now_dt = datetime.fromisoformat(ts)
            cutoff_dt = now_dt - timedelta(seconds=window)
            cutoff = cutoff_dt.isoformat(timespec="milliseconds")
            existing = conn.execute(
                "SELECT id FROM entity_references "
                "WHERE entity_id = ? AND source_path = ? "
                "AND source_kind = ? AND occurred_at >= ? "
                "ORDER BY occurred_at DESC LIMIT 1",
                (entity_id, source_path, source_kind, cutoff),
            ).fetchone()
            if existing:
                return existing["id"]

        conn.execute("BEGIN")
        cursor = conn.execute(
            "INSERT INTO entity_references "
            "(entity_id, source_path, source_kind, occurred_at, snippet) "
            "VALUES (?, ?, ?, ?, ?)",
            (entity_id, source_path, source_kind, ts, snippet),
        )
        ref_id = cursor.lastrowid
        # Touch the entity so list views reflect recency-of-mention.
        conn.execute(
            "UPDATE entities SET updated_at = ? WHERE id = ?",
            (ts, entity_id),
        )
        conn.commit()
        return ref_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_references(
    entity_id: int,
    *,
    limit: int | None = 50,
) -> list[dict[str, Any]]:
    """List references for an entity, newest first.

    Default ``limit`` is 50 to keep the dashboard responsive. Pass
    ``None`` for the full set.
    """
    conn = get_connection()
    try:
        sql = (
            "SELECT id, source_path, source_kind, occurred_at, snippet "
            "FROM entity_references WHERE entity_id = ? "
            "ORDER BY occurred_at DESC"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return [
            dict(r) for r in conn.execute(sql, (entity_id,))
        ]
    finally:
        conn.close()


def count_references(entity_id: int) -> int:
    """Return the total count of reference rows for an entity."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM entity_references WHERE entity_id = ?",
            (entity_id,),
        ).fetchone()
        return int(row["n"])
    finally:
        conn.close()
