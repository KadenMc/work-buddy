"""SQL schema for the durable summarization store.

Two tables: `summary_items` (one row per summarized item; provenance + status)
and `summary_nodes` (the tree itself; adjacency-list with `parent_id` and
`level`, plus a `source_ref` slot on every node).

The `namespace` column partitions rows so one DB can hold multiple
compositions (e.g. `conversation_session` and `chrome_page`) without
collisions on `item_id`.

Foreign-key cascade is NOT enforced via SQLite FKs (off by default). Rows are
managed by the store's own delete paths.
"""

from __future__ import annotations

SCHEMA = """\
CREATE TABLE IF NOT EXISTS summary_items (
    namespace               TEXT NOT NULL,
    item_id                 TEXT NOT NULL,
    freshness_token         TEXT NOT NULL,
    generated_at            TEXT NOT NULL,
    model                   TEXT,
    backend                 TEXT,
    profile                 TEXT,
    prompt_version          INTEGER NOT NULL,
    summary_schema_version  INTEGER NOT NULL,
    selection_version       INTEGER NOT NULL,
    cache_version           INTEGER NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'ok',
    error                   TEXT,
    PRIMARY KEY (namespace, item_id)
);

CREATE INDEX IF NOT EXISTS idx_summary_items_namespace
    ON summary_items(namespace);
CREATE INDEX IF NOT EXISTS idx_summary_items_generated_at
    ON summary_items(generated_at);


CREATE TABLE IF NOT EXISTS summary_nodes (
    id          TEXT PRIMARY KEY,            -- "{namespace}:{item_id}:{ordinal}"
    namespace   TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    parent_id   TEXT,                        -- NULL for the root node
    ordinal     INTEGER NOT NULL,            -- pre-order sibling order
    level       INTEGER NOT NULL,            -- 0 = root, 1 = child, ...
    summary     TEXT NOT NULL,
    source_ref  TEXT,                        -- JSON, nullable, on EVERY node
    extra_json  TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_summary_nodes_item
    ON summary_nodes(namespace, item_id, level, ordinal);
CREATE INDEX IF NOT EXISTS idx_summary_nodes_parent
    ON summary_nodes(parent_id);
"""
