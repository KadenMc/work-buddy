"""Unified MCP-facing dispatch over registered `TreeDrillable`s.

`drill_tree(domain, node_id, depth)` looks up the per-domain `TreeDrillable`
and returns the requested view as a JSON-friendly dict. Unknown domains and
malformed node ids surface as structured errors, not exceptions.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from work_buddy.disclosure.protocol import DrillError
from work_buddy.disclosure.registry import available_domains, get_drillable


def drill_tree(
    domain: str,
    node_id: str,
    depth: str = "summary",
) -> dict[str, Any]:
    """Walk a registered tree-shaped resource at the requested depth.

    Args:
        domain: Registered domain name. ``available_domains`` for the list
            (today: ``"knowledge"``, ``"summary"``).
        node_id: Domain-specific node identifier. Knowledge: a unit path
            (e.g. ``"architecture/summarization-framework"``). Summary:
            ``"{namespace}:{item_id}"`` for the whole tree or
            ``"{namespace}:{item_id}#n{ordinal}"`` for one node.
        depth: ``"index"`` (this node + child names), ``"summary"`` (this
            node + each child's summary), ``"full"`` (everything).

    Returns a dict containing the `TreeView` fields, or
    ``{"error": "...", "domain": ..., "available_domains": [...]}`` on
    failure.
    """
    if not domain or not isinstance(domain, str):
        return {
            "error": "domain is required",
            "available_domains": available_domains(),
        }

    try:
        drillable = get_drillable(domain)
    except KeyError as exc:
        return {
            "error": str(exc),
            "domain": domain,
            "available_domains": available_domains(),
        }

    try:
        view = drillable.get(node_id, depth=depth)
    except DrillError as exc:
        return {
            "error": str(exc),
            "domain": domain,
            "node_id": node_id,
            "depth": depth,
        }
    except Exception as exc:  # pragma: no cover — defensive
        return {
            "error": f"drillable.get crashed: {type(exc).__name__}: {exc}",
            "domain": domain,
            "node_id": node_id,
            "depth": depth,
        }

    return _view_to_dict(view)


def _view_to_dict(view: Any) -> dict[str, Any]:
    """Convert a `TreeView` to a plain dict for MCP transport."""
    d = asdict(view)
    return d
