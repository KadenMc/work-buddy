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
    # Canonical short-name alias `walk` registered alongside `drill_tree`.
    # Both bind the same callable; capability declarations live separately
    # so each gets its own discoverable name, parameter schema, and
    # content body. ``replace=True`` is set on both registrations so a
    # registry reload (importlib.reload via load_builtin_ops) re-binds
    # cleanly rather than crashing on the already-registered names —
    # important under pytest collection when test ordering triggers the
    # reload path.
    register_op("op.wb.drill_tree", drill_tree_op, replace=True)
    register_op("op.wb.walk", drill_tree_op, replace=True)


_register()
