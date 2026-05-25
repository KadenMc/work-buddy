"""`TreeDrillable` over the summarization framework's summary store.

Node-id formats:
- ``{namespace}:{item_id}`` — the whole item (the tree's root, e.g. one
  summarized session). Children are the level-1 topic nodes.
- ``{namespace}:{item_id}#n{ordinal}`` — a specific node within the tree.
  Children are its direct descendants (deeper trees aren't built today, so
  level-1 nodes have no children in practice).

The same node-id format is used in the IR `summary` source's `doc_id`
field (sans the `#n{ordinal}` suffix becoming `:n{ordinal}`), so a hit
from `summary_search` can be drilled by translating its `doc_id` to
`{namespace}:{item_id}#n{ordinal}`.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from work_buddy.disclosure.protocol import (
    ChildRef,
    DrillError,
    TreeDrillable,
    TreeView,
)


def _parse_node_id(node_id: str) -> tuple[str, str, int | None]:
    """Parse a node_id into `(namespace, inner_item_id, ordinal_or_None)`.

    Raises `DrillError` on malformed inputs.
    """
    if "#" in node_id:
        head, _, suffix = node_id.partition("#")
        if not suffix.startswith("n"):
            raise DrillError(
                f"summary node_id has '#...' suffix that isn't ordinal: "
                f"{node_id!r}"
            )
        try:
            ordinal = int(suffix[1:])
        except ValueError:
            raise DrillError(
                f"summary node_id ordinal not an int: {node_id!r}"
            ) from None
    else:
        head = node_id
        ordinal = None

    if ":" not in head:
        raise DrillError(
            f"summary node_id missing namespace prefix: {node_id!r}"
        )
    namespace, inner = head.split(":", 1)
    if not namespace or not inner:
        raise DrillError(f"summary node_id has empty parts: {node_id!r}")
    return namespace, inner, ordinal


class SummaryTreeDrillable:
    """`TreeDrillable` over `summary_items` + `summary_nodes`."""

    domain = "summary"

    def get(
        self,
        node_id: str,
        depth: str = "summary",
    ) -> TreeView:
        if depth not in ("index", "summary", "full"):
            raise DrillError(
                f"summary.get: invalid depth {depth!r} "
                f"(expected index|summary|full)"
            )

        from work_buddy.summarization.db import db_path

        namespace, inner_id, ordinal = _parse_node_id(node_id)
        path = db_path()
        if not Path(path).exists():
            raise DrillError(
                f"summary domain has no DB at {path}; index has not been "
                "produced yet."
            )

        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        try:
            item_row = conn.execute(
                "SELECT * FROM summary_items "
                "WHERE namespace = ? AND item_id = ?",
                (namespace, inner_id),
            ).fetchone()
            if item_row is None:
                raise DrillError(
                    f"summary item not found: {namespace}:{inner_id}"
                )
            all_nodes = list(conn.execute(
                "SELECT * FROM summary_nodes "
                "WHERE namespace = ? AND item_id = ? "
                "ORDER BY ordinal",
                (namespace, inner_id),
            ))
        finally:
            conn.close()

        if not all_nodes:
            # Error rows (record_error after no prior good summary) — no nodes.
            return TreeView(
                domain=self.domain,
                node_id=node_id,
                title=f"{namespace}:{inner_id}",
                depth=depth,
                summary_text=(
                    f"[no nodes; status={item_row['status']!r}"
                    + (f"; error={item_row['error']!r}"
                       if item_row["error"] else "")
                    + "]"
                ),
                full_text=None,
                children=[],
                parent_id=None,
                metadata={
                    "namespace": namespace,
                    "item_id": inner_id,
                    "status": item_row["status"],
                    "error": item_row["error"],
                    "generated_at": item_row["generated_at"],
                    "model": item_row["model"],
                },
            )

        by_ordinal: dict[int, dict[str, Any]] = {
            row["ordinal"]: dict(row) for row in all_nodes
        }
        children_of: dict[int | None, list[int]] = {}
        for row in all_nodes:
            parent_ord = self._ordinal_of(row["parent_id"])
            children_of.setdefault(parent_ord, []).append(row["ordinal"])

        target_ordinal = 0 if ordinal is None else ordinal
        if target_ordinal not in by_ordinal:
            raise DrillError(
                f"summary node ordinal {target_ordinal} not found "
                f"in {namespace}:{inner_id}"
            )
        node = by_ordinal[target_ordinal]

        return self._render(
            namespace=namespace,
            inner_id=inner_id,
            node=node,
            ordinal=target_ordinal,
            depth=depth,
            by_ordinal=by_ordinal,
            children_of=children_of,
            item_row=item_row,
        )

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _ordinal_of(parent_id: str | None) -> int | None:
        if parent_id is None:
            return None
        # parent_id format: "{namespace}:{item}:{ordinal}"
        try:
            return int(parent_id.rsplit(":", 1)[-1])
        except (ValueError, AttributeError):
            return None

    def _render(
        self,
        *,
        namespace: str,
        inner_id: str,
        node: dict[str, Any],
        ordinal: int,
        depth: str,
        by_ordinal: dict[int, dict[str, Any]],
        children_of: dict[int | None, list[int]],
        item_row: sqlite3.Row,
    ) -> TreeView:
        try:
            extra = json.loads(node.get("extra_json") or "{}")
        except (ValueError, TypeError):
            extra = {}
        try:
            source_ref = (
                json.loads(node["source_ref"])
                if node.get("source_ref")
                else None
            )
        except (ValueError, TypeError):
            source_ref = None

        title = str(extra.get("title") or "").strip()
        if ordinal == 0 and not title:
            title = f"{namespace}:{inner_id}"
        summary_text = node["summary"] or ""

        # children: at depth=index → just node_ids + titles; at depth=summary
        # → also summary_text; at depth=full → also build full subtree concat.
        children: list[ChildRef] = []
        for child_ord in children_of.get(ordinal, []):
            child = by_ordinal[child_ord]
            try:
                child_extra = json.loads(child.get("extra_json") or "{}")
            except (ValueError, TypeError):
                child_extra = {}
            child_title = (
                str(child_extra.get("title") or "").strip()
                or child["summary"][:60]
            )
            child_summary = (
                child["summary"] if depth in ("summary", "full") else None
            )
            children.append(ChildRef(
                node_id=f"{namespace}:{inner_id}#n{child_ord}",
                title=child_title,
                summary_text=child_summary,
            ))

        node_id_str = (
            f"{namespace}:{inner_id}"
            if ordinal == 0
            else f"{namespace}:{inner_id}#n{ordinal}"
        )

        parent_ord = self._ordinal_of(node.get("parent_id"))
        parent_id_str: str | None
        if parent_ord is None:
            parent_id_str = None
        elif parent_ord == 0:
            parent_id_str = f"{namespace}:{inner_id}"
        else:
            parent_id_str = f"{namespace}:{inner_id}#n{parent_ord}"

        full_text: str | None = None
        if depth == "full":
            # Subtree concat: this node's summary + every descendant's
            # title+summary in pre-order.
            full_text = self._concat_subtree(
                ordinal, by_ordinal, children_of,
            )

        return TreeView(
            domain=self.domain,
            node_id=node_id_str,
            title=title,
            depth=depth,
            summary_text=summary_text,
            full_text=full_text,
            children=children,
            parent_id=parent_id_str,
            metadata={
                "namespace": namespace,
                "item_id": inner_id,
                "level": node["level"],
                "ordinal": ordinal,
                "source_ref": source_ref,
                "extra": extra,
                "generated_at": item_row["generated_at"],
                "model": item_row["model"],
                "prompt_version": item_row["prompt_version"],
                "status": item_row["status"],
            },
        )

    @staticmethod
    def _concat_subtree(
        ordinal: int,
        by_ordinal: dict[int, dict[str, Any]],
        children_of: dict[int | None, list[int]],
    ) -> str:
        """Pre-order concat of node + descendants for full_text."""
        out_parts: list[str] = []

        def walk(ord_: int, indent: int) -> None:
            node = by_ordinal.get(ord_)
            if node is None:
                return
            try:
                extra = json.loads(node.get("extra_json") or "{}")
            except (ValueError, TypeError):
                extra = {}
            title = str(extra.get("title") or "").strip()
            prefix = "  " * indent
            if title:
                out_parts.append(f"{prefix}# {title}")
            if node["summary"]:
                out_parts.append(f"{prefix}{node['summary']}")
            for c in children_of.get(ord_, []):
                walk(c, indent + 1)

        walk(ordinal, 0)
        return "\n".join(out_parts)
