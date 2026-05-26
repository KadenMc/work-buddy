"""Progressive-disclosure navigation contract.

A `TreeDrillable` Protocol + a per-domain registry + a unified MCP
capability (`drill_tree`) that walks any registered tree-shaped resource
at three depths: ``index`` (node + child names), ``summary`` (node + child
summaries), ``full`` (everything).

Two concrete `TreeDrillable` implementations today:
- ``knowledge`` — wraps the knowledge store (`agent_docs`).
- ``summary`` — wraps the summarization framework's per-node summary store.

Sequence-shaped resources (session transcripts, workflow step logs) keep
their existing per-domain capabilities — the Protocol is deliberately
tree-shaped and does not force-fit them. Future tree-shaped domains
(doc/event-stream summarizers, Chrome page outlines, etc.) plug in by
implementing the Protocol and registering an instance.
"""

from work_buddy.disclosure.protocol import (
    ChildRef,
    DrillError,
    TreeDrillable,
    TreeView,
)
from work_buddy.disclosure.registry import (
    available_domains,
    get_drillable,
    register_drillable,
)

__all__ = [
    "ChildRef",
    "DrillError",
    "TreeDrillable",
    "TreeView",
    "available_domains",
    "get_drillable",
    "register_drillable",
]
