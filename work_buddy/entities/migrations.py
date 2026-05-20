"""Versioned schema migrations for the entity registry SQLite store.

Adopts the shared :class:`work_buddy.storage.migrations.MigrationRunner`
framework. Schema:

  v1 → ``entities``, ``entity_tags``, ``entity_aliases``,
       ``entity_references`` (append-only)

Migrations are idempotent. The ladder is intentionally short: the
entity subsystem is new and has no pre-framework legacy DB to baseline-
stamp. Future schema additions (entity-to-entity relations, alternative
description revisions, additional reference metadata) ride new
migrations on this ladder.
"""

from __future__ import annotations

import sqlite3

from work_buddy.logging_config import get_logger
from work_buddy.storage.migrations import Migration, MigrationRunner

logger = get_logger(__name__)


# ─── Migration 1 — initial schema ───────────────────────────────────


def _m001_initial_schema(conn: sqlite3.Connection) -> None:
    """Create the entity registry tables.

    Four tables, each idempotent via ``CREATE TABLE IF NOT EXISTS``:

    ``entities`` — one row per canonical entity. Surrogate integer
    ``id`` PK is the stable identifier. ``canonical_name`` carries the
    display string; ``canonical_norm`` carries the case-folded form
    used for lookup and uniqueness. No soft-delete: the entity is
    either present or hard-deleted. No revision history: descriptions
    are last-write-wins prose; future opt-in revisioning can ride a
    new migration.

    ``entity_tags`` — hierarchical, multi-valued. A tag like
    ``person/family`` is stored as-is in ``tag_norm`` (lowercased,
    slashes preserved). Hierarchical filter is a prefix match on the
    LIKE side; see :func:`work_buddy.entities.store.list_entities`.

    ``entity_aliases`` — alternative names that resolve to the
    canonical row. ``alias_norm`` is globally unique across all
    entities (an alias can belong to exactly one entity).

    ``entity_references`` — append-only mention log. One row per
    ``(entity_id, source_path, source_kind, occurred_at)`` event.
    Nothing deletes a reference except a cascade from deleting the
    parent entity. This is what gives the "historical references
    survive document evolution" property.

    Cascade policy: ON DELETE CASCADE on all child tables. Deleting
    an entity destroys its tags, aliases, and references in one step.
    The handoff envisioned references "surviving document evolution,"
    not "surviving entity destruction" — those are different concerns
    and conflating them would force a soft-delete state machine for
    no operational benefit.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS entities (
            id              INTEGER PRIMARY KEY,
            canonical_name  TEXT NOT NULL,
            canonical_norm  TEXT NOT NULL UNIQUE,
            description     TEXT,
            author          TEXT NOT NULL CHECK(author IN ('user','agent')),
            created_at      TEXT NOT NULL CHECK(created_at GLOB
                '????-??-??T??:??:??*'),
            updated_at      TEXT NOT NULL CHECK(updated_at GLOB
                '????-??-??T??:??:??*')
        );
        CREATE INDEX IF NOT EXISTS idx_entities_canonical_norm
            ON entities(canonical_norm);
        CREATE INDEX IF NOT EXISTS idx_entities_updated_at
            ON entities(updated_at DESC);

        CREATE TABLE IF NOT EXISTS entity_tags (
            id              INTEGER PRIMARY KEY,
            entity_id       INTEGER NOT NULL
                            REFERENCES entities(id) ON DELETE CASCADE,
            tag             TEXT NOT NULL,
            tag_norm        TEXT NOT NULL,
            UNIQUE(entity_id, tag_norm)
        );
        CREATE INDEX IF NOT EXISTS idx_entity_tags_entity_id
            ON entity_tags(entity_id);
        CREATE INDEX IF NOT EXISTS idx_entity_tags_tag_norm
            ON entity_tags(tag_norm);

        CREATE TABLE IF NOT EXISTS entity_aliases (
            id              INTEGER PRIMARY KEY,
            entity_id       INTEGER NOT NULL
                            REFERENCES entities(id) ON DELETE CASCADE,
            alias           TEXT NOT NULL,
            alias_norm      TEXT NOT NULL UNIQUE
        );
        CREATE INDEX IF NOT EXISTS idx_entity_aliases_entity_id
            ON entity_aliases(entity_id);

        CREATE TABLE IF NOT EXISTS entity_references (
            id              INTEGER PRIMARY KEY,
            entity_id       INTEGER NOT NULL
                            REFERENCES entities(id) ON DELETE CASCADE,
            source_path     TEXT NOT NULL,
            source_kind     TEXT NOT NULL,
            occurred_at     TEXT NOT NULL CHECK(occurred_at GLOB
                '????-??-??T??:??:??*'),
            snippet         TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_entity_references_entity_id
            ON entity_references(entity_id, occurred_at DESC);
        CREATE INDEX IF NOT EXISTS idx_entity_references_source_path
            ON entity_references(source_path);
        """
    )


# ─── Runner instance ────────────────────────────────────────────────


ENTITY_MIGRATIONS = MigrationRunner(
    "entities",
    migrations=[
        Migration(
            1, "initial schema: entities, tags, aliases, references",
            _m001_initial_schema,
        ),
    ],
)
