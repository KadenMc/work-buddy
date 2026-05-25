"""`TreeDrillable` over the knowledge store.

Thin wrapper that maps a knowledge-unit path → the three `TreeView`
depths by delegating to `agent_docs`. Lets agents walk the knowledge
store using the same `drill_tree` capability they use for summaries
and any future tree-shaped domain.

Cost notes:
- At depth ``summary``, populating `summary_text` for each child requires
  one `agent_docs` call per child (the engine doesn't have a bulk
  "names + descriptions for these N paths" API yet). For typical knowledge
  subtrees (<~20 children) this is acceptable. If profiling shows this is
  a hot path, add a bulk fetch to `knowledge.query` and switch this wrapper
  to use it — the Protocol contract doesn't change.
"""

from __future__ import annotations

from typing import Any

from work_buddy.disclosure.protocol import (
    ChildRef,
    DrillError,
    TreeDrillable,
    TreeView,
)


class KnowledgeTreeDrillable:
    """`TreeDrillable` over the knowledge store via `agent_docs`."""

    domain = "knowledge"

    def get(self, node_id: str, depth: str = "summary") -> TreeView:
        if depth not in ("index", "summary", "full"):
            raise DrillError(
                f"knowledge.get: invalid depth {depth!r} "
                f"(expected index|summary|full)"
            )
        if not node_id or not isinstance(node_id, str):
            raise DrillError(f"knowledge.get: empty/invalid node_id {node_id!r}")

        unit = self._lookup(node_id, depth)
        if unit is None:
            raise DrillError(f"knowledge unit not found: {node_id!r}")

        children_paths: list[str] = list(unit.get("children") or [])
        children: list[ChildRef] = []
        if depth == "index":
            for cp in children_paths:
                children.append(ChildRef(
                    node_id=cp,
                    title=self._title_of(cp),
                    summary_text=None,
                ))
        else:
            # depth in ("summary", "full") — fetch each child at depth=summary
            # for its description / name.
            for cp in children_paths:
                child = self._lookup(cp, "summary")
                if child is None:
                    children.append(ChildRef(
                        node_id=cp,
                        title=self._title_of(cp),
                        summary_text=None,
                    ))
                    continue
                children.append(ChildRef(
                    node_id=cp,
                    title=str(child.get("name") or cp),
                    summary_text=str(child.get("description") or ""),
                ))

        # Parent id: pick the first parent if any (knowledge units allow
        # multiple). Surface the full list in metadata.
        parents = list(unit.get("parents") or [])
        parent_id_str = parents[0] if parents else None

        full_text: str | None = None
        if depth == "full":
            full_text = str(unit.get("content") or "")

        metadata: dict[str, Any] = {
            "kind": unit.get("kind"),
            "parents": parents,
            "tags": list(unit.get("tags") or []),
            "aliases": list(unit.get("aliases") or []),
        }
        # Pass through capability-specific frontmatter when present so
        # consumers can act on it (e.g. `op`, `category`, `command`).
        for key in (
            "op", "category", "schema_version", "command", "workflow",
            "capability_name", "trigger", "mutates_state", "retry_policy",
            "entry_points", "consent_required",
        ):
            if key in unit and unit[key] is not None:
                metadata[key] = unit[key]

        return TreeView(
            domain=self.domain,
            node_id=node_id,
            title=str(unit.get("name") or node_id),
            depth=depth,
            summary_text=str(unit.get("description") or ""),
            full_text=full_text,
            children=children,
            parent_id=parent_id_str,
            metadata=metadata,
        )

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _lookup(path: str, depth: str) -> dict[str, Any] | None:
        """Call `agent_docs(path=, depth=)` and return the unit dict (or
        None on miss). Imports lazily — knowledge module pulls in store
        readers + the search index."""
        from work_buddy.knowledge.query import agent_docs

        try:
            resp = agent_docs(path=path, depth=depth)
        except Exception:
            return None
        if not isinstance(resp, dict):
            return None
        if resp.get("mode") != "lookup":
            return None
        unit = resp.get("unit")
        if not isinstance(unit, dict):
            return None
        return unit

    @staticmethod
    def _title_of(path: str) -> str:
        """Cheap fallback title for a path when we haven't loaded the unit."""
        return path.rsplit("/", 1)[-1]
