"""`TreeDrillable` over the summarization framework's summary store.

Node-id formats:
- ``{namespace}`` (no colon) — the namespace itself. Children are all
  summary items under that namespace, ordered by `generated_at` DESC.
  Useful for "show me every summarized session" discovery.
- ``{namespace}:{item_id}`` — the whole item (the tree's root, e.g. one
  summarized session). Children are the level-1 topic nodes.
- ``{namespace}:{item_id}#n{ordinal}`` — a specific node within the tree.
  Children are its direct descendants (deeper trees aren't built today, so
  level-1 nodes have no children in practice).

The IR `summary` source's `doc_id` field uses `{namespace}:{item_id}:n{ordinal}`;
translate to a drill node_id by swapping the last `:n` for `#n`.
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


def _parse_node_id(node_id: str) -> tuple[str, str | None, int | None]:
    """Parse a node_id into `(namespace, inner_item_id|None, ordinal|None)`.

    Three valid shapes:
    - ``{namespace}`` → `(ns, None, None)` — namespace root.
    - ``{namespace}:{item}`` → `(ns, item, None)` — item root.
    - ``{namespace}:{item}#n{ord}`` → `(ns, item, ord)` — internal node.

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
            ordinal: int | None = int(suffix[1:])
        except ValueError:
            raise DrillError(
                f"summary node_id ordinal not an int: {node_id!r}"
            ) from None
    else:
        head = node_id
        ordinal = None

    if not head:
        raise DrillError(f"summary node_id is empty: {node_id!r}")

    if ":" not in head:
        # Pure namespace — no item id, must not have ordinal.
        if ordinal is not None:
            raise DrillError(
                f"summary node_id cannot specify ordinal on a namespace: "
                f"{node_id!r}"
            )
        return head, None, None

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

        if inner_id is None:
            # Namespace-root view: list all items as children.
            return self._namespace_view(namespace, depth, path)

        return self._item_view(namespace, inner_id, ordinal, depth, path)

    # --------------------------------------------------------------- views

    def _namespace_view(
        self, namespace: str, depth: str, path: Any,
    ) -> TreeView:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        try:
            rows = list(conn.execute(
                "SELECT item_id, generated_at, model, status "
                "FROM summary_items "
                "WHERE namespace = ? "
                "ORDER BY generated_at DESC",
                (namespace,),
            ))
        finally:
            conn.close()

        children: list[ChildRef] = []
        for r in rows:
            child_summary_text: str | None = None
            if depth in ("summary", "full"):
                # Lazy per-child fetch for the root summary text. Cost is
                # one query per item — acceptable for the namespace-list
                # view, which is bounded by how many summarized items exist.
                child_summary_text = self._root_summary_text(
                    namespace, r["item_id"], path,
                )
            title = f"{namespace}:{r['item_id']}"
            children.append(ChildRef(
                node_id=title,
                title=title,
                summary_text=child_summary_text,
            ))

        return TreeView(
            domain=self.domain,
            node_id=namespace,
            title=namespace,
            depth=depth,
            summary_text=(
                f"{len(rows)} summarized item(s) in namespace "
                f"{namespace!r}"
            ),
            full_text=None,
            children=children,
            parent_id=None,
            metadata={
                "namespace": namespace,
                "item_count": len(rows),
            },
        )

    def _item_view(
        self,
        namespace: str,
        inner_id: str,
        ordinal: int | None,
        depth: str,
        path: Any,
    ) -> TreeView:
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
            return TreeView(
                domain=self.domain,
                node_id=f"{namespace}:{inner_id}",
                title=f"{namespace}:{inner_id}",
                depth=depth,
                summary_text=(
                    f"[no nodes; status={item_row['status']!r}"
                    + (
                        f"; error={item_row['error']!r}"
                        if item_row["error"]
                        else ""
                    )
                    + "]"
                ),
                full_text=None,
                children=[],
                parent_id=namespace,
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

    @staticmethod
    def _root_summary_text(
        namespace: str, item_id: str, path: Any,
    ) -> str | None:
        """Fetch the root node's summary text for an item — used by the
        namespace-root view to populate per-child `summary_text`."""
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT summary FROM summary_nodes "
                "WHERE namespace = ? AND item_id = ? AND ordinal = 0",
                (namespace, item_id),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return row["summary"]

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
        if ordinal == 0:
            # Root's parent is the namespace.
            parent_id_str = namespace
        elif parent_ord is None:
            parent_id_str = None
        elif parent_ord == 0:
            parent_id_str = f"{namespace}:{inner_id}"
        else:
            parent_id_str = f"{namespace}:{inner_id}#n{parent_ord}"

        full_text: str | None = None
        if depth == "full":
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
