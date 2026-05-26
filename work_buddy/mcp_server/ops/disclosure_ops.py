"""Disclosure-domain ops — the unified `drill_tree` capability.

`drill_tree` walks any registered `TreeDrillable` at three depths
(index/summary/full). Today's domains: ``knowledge`` and ``summary``;
adding a new tree-shaped resource is one `register_drillable` call.
"""

from __future__ import annotations

from typing import Any

from work_buddy.mcp_server.op_registry import register_op


def drill_tree_op(
    domain: str,
    node_id: str,
    depth: str = "index",
) -> dict[str, Any]:
    """Walk a tree-shaped drillable resource at the requested depth."""
    from work_buddy.disclosure.dispatch import drill_tree

    return drill_tree(domain, node_id, depth=depth)


def _register() -> None:
    register_op("op.wb.drill_tree", drill_tree_op)


_register()
