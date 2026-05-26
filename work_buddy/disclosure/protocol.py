"""`TreeDrillable` Protocol + the view dataclasses agents get back.

Three depths:

- ``index`` — node identity + the list of child names only. Cheapest. Useful
  when you only need to know *what's available* under this node.
- ``summary`` — node + each child's name + each child's `summary_text`.
  Right for triage: "which of these matters?" without paying for full
  content.
- ``full`` — node + everything: full content of this node and (optionally,
  shallow) of its children. The agent has the actual material in hand.

The Protocol is deliberately narrow: one method, `get(node_id, depth)`,
plus a `domain` identifier. Sequence-shaped resources (sessions,
workflow logs) are not force-fit — they keep their own surface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Depth = Literal["index", "summary", "full"]


class DrillError(Exception):
    """Raised by a `TreeDrillable` when an input is malformed or the
    requested node does not exist. The dispatch capability catches this
    and returns a structured error to the agent."""


@dataclass
class ChildRef:
    """A pointer to a child node under a `TreeView`.

    `summary_text` is populated only at depth ``summary`` or ``full``;
    at ``index`` it is `None` (callers only need the name list).
    """

    node_id: str
    title: str
    summary_text: str | None = None


@dataclass
class TreeView:
    """A response from `TreeDrillable.get`. One node, with depth-conditional
    payload + a list of child references (when the node has children).
    """

    domain: str
    node_id: str
    title: str
    depth: Depth
    summary_text: str = ""
    full_text: str | None = None  # populated at depth="full"
    children: list[ChildRef] = field(default_factory=list)
    parent_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class TreeDrillable(Protocol):
    """A tree-shaped drillable resource.

    `node_id` is domain-specific (a knowledge-unit path; a
    ``{namespace}:{item_id}`` summary id; a node-within-tree id). The
    domain owns the format; consumers treat it as opaque.

    Implementations should raise `DrillError` for malformed input or
    missing nodes; the dispatch capability turns it into a structured
    error response.
    """

    domain: str

    def get(self, node_id: str, depth: Depth = "summary") -> TreeView:
        """Return a view of `node_id` at the requested depth."""
        ...
