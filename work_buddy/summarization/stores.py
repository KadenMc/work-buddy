"""The two `Store` implementations.

- `DurableSummaryStore` — version-stamped SQLite persistence of arbitrary-
  depth `SummaryNode` trees. The conv_obs composition uses this. One DB can
  hold multiple compositions (partitioned by `namespace`).
- `TtlCacheStore` — wraps the existing `work_buddy.llm.cache` (content-hash +
  SimHash + TTL). The Chrome composition uses this. Persists flat or shallow
  trees opaquely as a JSON blob.

Both share one private staleness predicate per store class so `is_fresh` and
`select_stale` cannot diverge.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from pathlib import Path
from typing import Any

from work_buddy.summarization.db import db_path as _default_db_path_fn
from work_buddy.summarization.db import get_connection
from work_buddy.summarization.protocol import (
    Provenance,
    SummaryCapability,
    SummaryNode,
)

logger = logging.getLogger(__name__)


# ===========================================================================
# DurableSummaryStore — SQLite, version-stamped, tree-shaped
# ===========================================================================


class DurableSummaryStore:
    """Durable SQLite store for `SummaryNode` trees.

    Constructed with `(namespace, selection_version, cache_version,
    db_path?)`. `namespace` partitions rows in `summary_items` /
    `summary_nodes` so multiple compositions can share one DB file.

    Stale criteria (the shared private predicate `_is_stale_row`):
    1. No row in `summary_items` for `(namespace, item_id)`.
    2. Any of the four version ints differs from the configured values.
    3. The stored `freshness_token` differs from the candidate's token.
    4. `status != 'ok'`.
    """

    capabilities = frozenset({
        SummaryCapability.PERSISTS_TREE,
        SummaryCapability.VERSION_STAMPED,
    })

    def __init__(
        self,
        namespace: str,
        *,
        selection_version: int = 1,
        cache_version: int = 1,
        db_path: Path | None = None,
    ) -> None:
        self.name = f"durable:{namespace}"
        self.namespace = namespace
        self.selection_version = selection_version
        self.cache_version = cache_version
        self._db_path_override = db_path
        # Strategy-side versions are set by the Summarizer composer post
        # construction via `set_strategy_versions`. Default to 1 so a
        # standalone store still works for tests; the composer wires real
        # values from the paired strategy.
        self._strategy_prompt_version: int = 1
        self._strategy_schema_version: int = 1

    def set_strategy_versions(
        self, prompt_version: int, schema_version: int,
    ) -> None:
        """Called by the `Summarizer` composer to bridge the strategy's
        version stamps into the store's staleness check. Without this,
        bumping a strategy's `prompt_version` wouldn't invalidate stored
        rows (the store would compare against its default).
        """
        self._strategy_prompt_version = prompt_version
        self._strategy_schema_version = schema_version

    # ---------------------------------------------------------------- helpers

    def _connect(self):
        if self._db_path_override is not None:
            return get_connection(cfg={
                "summarization": {"db_path": str(self._db_path_override)},
            })
        return get_connection()

    def _is_stale_row(
        self,
        row: Any,
        current_token: Any,
    ) -> bool:
        """Single source of truth for staleness. `row` may be a sqlite3.Row or
        None (missing). `current_token` is the candidate's freshness token."""
        if row is None:
            return True
        if row["status"] != "ok":
            return True
        if (
            row["prompt_version"] != self._strategy_prompt_version
            or row["summary_schema_version"]
            != self._strategy_schema_version
            or row["selection_version"] != self.selection_version
            or row["cache_version"] != self.cache_version
        ):
            return True
        return str(row["freshness_token"]) != str(current_token)

    # ---------------------------------------------------------------- staleness

    def is_fresh(self, item_id: str, freshness_token: Any) -> bool:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM summary_items "
                "WHERE namespace = ? AND item_id = ?",
                (self.namespace, item_id),
            ).fetchone()
        finally:
            conn.close()
        return not self._is_stale_row(row, freshness_token)

    def select_stale(
        self,
        candidates: list[tuple[str, Any]],
    ) -> list[tuple[str, Any]]:
        if not candidates:
            return []
        conn = self._connect()
        try:
            rows_by_id: dict[str, Any] = {}
            for row in conn.execute(
                "SELECT * FROM summary_items WHERE namespace = ?",
                (self.namespace,),
            ):
                rows_by_id[row["item_id"]] = row
        finally:
            conn.close()
        return [
            (iid, token)
            for iid, token in candidates
            if self._is_stale_row(rows_by_id.get(iid), token)
        ]

    # ---------------------------------------------------------------- write

    def save(
        self,
        item_id: str,
        result: SummaryNode,
        provenance: Provenance,
        freshness_token: Any,
    ) -> None:
        conn = self._connect()
        try:
            # Replace any prior nodes for this item.
            conn.execute(
                "DELETE FROM summary_nodes "
                "WHERE namespace = ? AND item_id = ?",
                (self.namespace, item_id),
            )

            # Walk tree pre-order, assigning ordinals + parent ids.
            ordinal_counter = 0
            stack: list[tuple[SummaryNode, str | None]] = [(result, None)]
            # We need to insert in DFS pre-order, but a stack reverses children.
            # Reverse children when pushing so we visit them in original order.
            inserted_root_id: str | None = None
            # Use iterative DFS with parent tracking.
            work: list[tuple[SummaryNode, str | None, int]] = [(result, None, 0)]
            while work:
                node, parent_id, level = work.pop(0)
                node_id = f"{self.namespace}:{item_id}:{ordinal_counter}"
                if inserted_root_id is None:
                    inserted_root_id = node_id
                conn.execute(
                    "INSERT INTO summary_nodes "
                    "(id, namespace, item_id, parent_id, ordinal, level, "
                    " summary, source_ref, extra_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        node_id,
                        self.namespace,
                        item_id,
                        parent_id,
                        ordinal_counter,
                        level,
                        node.summary,
                        json.dumps(node.source_ref, ensure_ascii=False)
                        if node.source_ref is not None
                        else None,
                        json.dumps(node.extra or {}, ensure_ascii=False),
                    ),
                )
                ordinal_counter += 1
                # Append children (preserves order).
                for child in node.children:
                    work.append((child, node_id, level + 1))

            # Upsert the items row.
            conn.execute(
                "INSERT INTO summary_items "
                "(namespace, item_id, freshness_token, generated_at, "
                " model, backend, profile, prompt_version, "
                " summary_schema_version, selection_version, cache_version, "
                " status, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ok', NULL) "
                "ON CONFLICT(namespace, item_id) DO UPDATE SET "
                "  freshness_token=excluded.freshness_token, "
                "  generated_at=excluded.generated_at, "
                "  model=excluded.model, backend=excluded.backend, "
                "  profile=excluded.profile, "
                "  prompt_version=excluded.prompt_version, "
                "  summary_schema_version=excluded.summary_schema_version, "
                "  selection_version=excluded.selection_version, "
                "  cache_version=excluded.cache_version, "
                "  status='ok', error=NULL",
                (
                    self.namespace,
                    item_id,
                    str(freshness_token),
                    provenance.generated_at,
                    provenance.model,
                    provenance.backend,
                    provenance.profile,
                    provenance.prompt_version,
                    provenance.summary_schema_version,
                    provenance.selection_version,
                    provenance.cache_version,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def record_error(
        self,
        item_id: str,
        error: str,
        provenance: Provenance,
    ) -> None:
        """Stamp an error status without overwriting prior good nodes."""
        conn = self._connect()
        try:
            existing = conn.execute(
                "SELECT 1 FROM summary_items "
                "WHERE namespace = ? AND item_id = ?",
                (self.namespace, item_id),
            ).fetchone()
            if existing is None:
                # No prior row — insert a placeholder error row.
                conn.execute(
                    "INSERT INTO summary_items "
                    "(namespace, item_id, freshness_token, generated_at, "
                    " model, backend, profile, prompt_version, "
                    " summary_schema_version, selection_version, "
                    " cache_version, status, error) "
                    "VALUES (?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, 'error', ?)",
                    (
                        self.namespace, item_id,
                        provenance.generated_at,
                        provenance.model, provenance.backend, provenance.profile,
                        provenance.prompt_version,
                        provenance.summary_schema_version,
                        provenance.selection_version,
                        provenance.cache_version,
                        error,
                    ),
                )
            else:
                # Keep nodes intact; just flip status.
                conn.execute(
                    "UPDATE summary_items SET status='error', error=? "
                    "WHERE namespace = ? AND item_id = ?",
                    (error, self.namespace, item_id),
                )
            conn.commit()
        finally:
            conn.close()

    # ---------------------------------------------------------------- read

    def load(self, item_id: str) -> SummaryNode | None:
        conn = self._connect()
        try:
            item_row = conn.execute(
                "SELECT * FROM summary_items "
                "WHERE namespace = ? AND item_id = ?",
                (self.namespace, item_id),
            ).fetchone()
            if item_row is None:
                return None

            node_rows = list(conn.execute(
                "SELECT * FROM summary_nodes "
                "WHERE namespace = ? AND item_id = ? "
                "ORDER BY ordinal",
                (self.namespace, item_id),
            ))
        finally:
            conn.close()

        if not node_rows:
            return None

        # Build node objects and parent map.
        nodes_by_id: dict[str, SummaryNode] = {}
        order: list[tuple[str, str | None]] = []
        for row in node_rows:
            try:
                source_ref = (
                    json.loads(row["source_ref"])
                    if row["source_ref"] is not None
                    else None
                )
            except (ValueError, TypeError):
                source_ref = None
            try:
                extra = json.loads(row["extra_json"] or "{}")
            except (ValueError, TypeError):
                extra = {}

            nodes_by_id[row["id"]] = SummaryNode(
                summary=row["summary"],
                source_ref=source_ref,
                children=[],
                extra=extra,
            )
            order.append((row["id"], row["parent_id"]))

        # Wire up children. The pre-order ordinal guarantees parents appear
        # before their children.
        root: SummaryNode | None = None
        for node_id, parent_id in order:
            if parent_id is None:
                root = nodes_by_id[node_id]
            else:
                parent = nodes_by_id.get(parent_id)
                if parent is not None:
                    parent.children.append(nodes_by_id[node_id])

        return root

    def load_item_meta(self, item_id: str) -> dict[str, Any] | None:
        """Return the `summary_items` row as a dict (with `topic_count`
        derived from child nodes). Used by conv_obs shims to assemble the
        legacy result row without re-loading the whole tree twice."""
        conn = self._connect()
        try:
            item_row = conn.execute(
                "SELECT * FROM summary_items "
                "WHERE namespace = ? AND item_id = ?",
                (self.namespace, item_id),
            ).fetchone()
            if item_row is None:
                return None
            topic_count = conn.execute(
                "SELECT COUNT(*) AS n FROM summary_nodes "
                "WHERE namespace = ? AND item_id = ? AND level = 1",
                (self.namespace, item_id),
            ).fetchone()["n"]
        finally:
            conn.close()
        d = dict(item_row)
        d["topic_count"] = topic_count
        return d


# ===========================================================================
# TtlCacheStore — wraps work_buddy.llm.cache
# ===========================================================================


class TtlCacheStore:
    """TTL content-hash cache store, wrapping `work_buddy.llm.cache`.

    Constructed `(namespace, strategy_version_tag, ttl_minutes, *,
    key_prefix=None)`. The `strategy_version_tag` (e.g. `"chrome_page:v1"`)
    derives the `system_hash` so that a strategy prompt-version bump cleanly
    invalidates the cache.

    `freshness_token` shape: a dict `{"hash": <sha256 of content>, "text":
    <content>}`. The `hash` is the exact-match invalidation key; the `text`
    is used for `llm.cache`'s SimHash fuzzy fallback. The store stringifies
    nothing — the token is passed straight through.

    Persists trees opaquely as a JSON blob inside the cache entry's `result`,
    so it can hold either flat (depth-1) or shallow trees if a layered
    strategy ever pairs with a TTL store (today it does not — coherence checks
    require PERSISTS_TREE for LAYERED, and `TtlCacheStore` declares
    `PERSISTS_TREE` as well to keep the option open).
    """

    capabilities = frozenset({
        SummaryCapability.PERSISTS_FLAT,
        SummaryCapability.PERSISTS_TREE,
        SummaryCapability.TTL_EVICTED,
    })

    def __init__(
        self,
        namespace: str,
        *,
        strategy_version_tag: str,
        ttl_minutes: int = 30,
        key_prefix: str | None = None,
    ) -> None:
        self.name = f"ttl_cache:{namespace}"
        self.namespace = namespace
        self.strategy_version_tag = strategy_version_tag
        self.ttl_minutes = ttl_minutes
        # `key_prefix` lets the binding tweak the cache key (e.g. Chrome wants
        # `summarize_tab:` to preserve its existing cache scheme exactly).
        self.key_prefix = key_prefix or f"summarization:{namespace}"
        # TTL stores have their own version dimension — bump if persistence
        # shape changes incompatibly.
        self.selection_version = 1
        self.cache_version = 1
        self._system_hash = hashlib.sha256(
            strategy_version_tag.encode("utf-8")
        ).hexdigest()[:12]

    # ---------------------------------------------------------------- helpers

    def _scoped_key(self, item_id: str) -> str:
        return f"{self.key_prefix}:{item_id}"

    @staticmethod
    def _token_parts(freshness_token: Any) -> tuple[str, str | None]:
        """Extract `(input_hash, input_text)` from a token.

        Accepts a dict `{"hash": ..., "text": ...}` or a bare string (treated
        as the hash with no fuzzy text available)."""
        if isinstance(freshness_token, dict):
            h = str(freshness_token.get("hash", ""))
            t = freshness_token.get("text")
            return h, (str(t) if t is not None else None)
        return str(freshness_token), None

    # ---------------------------------------------------------------- staleness

    def is_fresh(self, item_id: str, freshness_token: Any) -> bool:
        from work_buddy.llm.cache import get as cache_get

        input_hash, input_text = self._token_parts(freshness_token)
        entry = cache_get(
            self._scoped_key(item_id),
            input_hash=input_hash,
            input_text=input_text,
        )
        if entry is None:
            return False
        # Same strategy_version_tag check — system_hash on the entry must
        # match. `cache_get` already filters via the scoped key, but as a
        # belt-and-braces invariant check:
        if entry.get("system_hash") != self._system_hash:
            return False
        return True

    def select_stale(
        self,
        candidates: list[tuple[str, Any]],
    ) -> list[tuple[str, Any]]:
        return [
            (iid, token)
            for iid, token in candidates
            if not self.is_fresh(iid, token)
        ]

    # ---------------------------------------------------------------- write

    def save(
        self,
        item_id: str,
        result: SummaryNode,
        provenance: Provenance,
        freshness_token: Any,
    ) -> None:
        from work_buddy.llm.cache import put as cache_put

        input_hash, input_text = self._token_parts(freshness_token)
        if input_text is None:
            # Cache requires input_text for SimHash; fall back to a stub.
            input_text = input_hash or ""

        cache_put(
            self._scoped_key(item_id),
            result={
                "tree": _node_to_jsonable(result),
                "provenance": _provenance_to_dict(provenance),
            },
            input_hash=input_hash,
            input_text=input_text,
            system_hash=self._system_hash,
            system_preview=self.strategy_version_tag,
            ttl_minutes=self.ttl_minutes,
            model=provenance.model or "",
        )

    def record_error(
        self,
        item_id: str,
        error: str,
        provenance: Provenance,
    ) -> None:
        # TTL cache: errors are not persisted. The next call retries.
        # Log so a debugger can see what happened.
        logger.warning(
            "TtlCacheStore[%s] error for %s: %s",
            self.namespace, item_id, error,
        )

    # ---------------------------------------------------------------- read

    def load(self, item_id: str) -> SummaryNode | None:
        from work_buddy.llm.cache import _read_cache  # type: ignore[attr-defined]

        # The cache.get() path requires `input_hash`. For load() in the
        # framework, we don't have the token at hand. Read the underlying
        # store directly and reconstruct the node.
        cache = _read_cache()
        entry = cache.get(self._scoped_key(item_id))
        if entry is None:
            return None
        if "input_hash" not in entry:
            return None  # legacy-schema entry — ignore
        if entry.get("system_hash") != self._system_hash:
            return None
        result = entry.get("result") or {}
        tree = result.get("tree")
        if not isinstance(tree, dict):
            return None
        return _node_from_jsonable(tree)


# ---------------------------------------------------------------------------
# Tree (de)serialization helpers
# ---------------------------------------------------------------------------


def _node_to_jsonable(node: SummaryNode) -> dict[str, Any]:
    return {
        "summary": node.summary,
        "source_ref": node.source_ref,
        "extra": node.extra or {},
        "children": [_node_to_jsonable(c) for c in node.children],
    }


def _node_from_jsonable(d: dict[str, Any]) -> SummaryNode:
    return SummaryNode(
        summary=str(d.get("summary", "")),
        source_ref=d.get("source_ref"),
        children=[_node_from_jsonable(c) for c in (d.get("children") or [])],
        extra=dict(d.get("extra") or {}),
    )


def _provenance_to_dict(p: Provenance) -> dict[str, Any]:
    return {
        "model": p.model,
        "backend": p.backend,
        "profile": p.profile,
        "generated_at": p.generated_at,
        "prompt_version": p.prompt_version,
        "summary_schema_version": p.summary_schema_version,
        "selection_version": p.selection_version,
        "cache_version": p.cache_version,
    }
