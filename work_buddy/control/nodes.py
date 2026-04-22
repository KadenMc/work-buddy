"""Dataclasses for the unified control graph.

A :class:`ControlNode` is one vertex. Its kind determines which state
projections apply:

    domain       grouping only — no preference, state rolled up from children
    subsystem    grouping with dependency edges — state rolled up
    component    leaf runtime entity — carries preference + health
    requirement  configuration check under a component — derived from check
    capability   registered capability or workflow — derived from requires/invokes

Edges come in two flavors and live on different fields:

    grouping_parents   "I roll up into X"   (hierarchy, no implied health contract)
    dependencies       "I need X healthy"   (runtime contract; drives blocked/disabled)

``effective_state`` is the single derived field the UI renders. It
fuses preference, requirement/health/probe signals, and dependency
state into one of six labels. The raw inputs remain queryable via the
existing ``/api/state`` and ``/api/requirements`` endpoints for
debugging.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

NodeKind = Literal["domain", "subsystem", "component", "requirement", "capability"]

EffectiveState = Literal[
    "ok",             # wanted + deps ok + config ok + probe healthy
    "degraded",       # wanted + deps ok + recommended config fails OR probe unavailable
    "blocked",        # wanted + a hard dependency is not ok
    "disabled",       # preference unwanted (cascades to children), or deps all disabled
    "unconfigured",   # wanted + required config check failed
    "unknown",        # no signals available yet — undecided preference or missing probe data
]

Preference = Literal["wanted", "unwanted", "undecided", "required"]
# "required" means the component is core (is_core=True in COMPONENT_CATALOG)
# and cannot be opted out of. UI should show a "Required" badge and hide
# the toggle. For cascade purposes "required" behaves identically to
# "wanted" — the component is always treated as active.


@dataclass(frozen=True)
class Edge:
    """Dependency edge between control nodes.

    ``mode='any'`` (deferred) is reserved for future "LM Studio OR Ollama"
    style fan-in. Phase A honors only ``mode='all'`` — every listed target
    must be healthy for the edge to be satisfied.

    ``hardness`` distinguishes two semantic flavors:

      - **hard** (default): if the target is down, *this node cannot
        function*. Failure cascades as ``blocked`` up the chain.
        Example: ``component:hindsight → component:postgresql`` —
        Hindsight literally cannot start without PostgreSQL.
      - **soft**: if the target is down, *this node works with reduced
        functionality*. Failure cascades as ``degraded`` at worst; if
        the target is merely ``disabled``, nothing propagates.
        Example: ``component:dashboard → component:embedding`` —
        the dashboard falls back to substring search without embeddings;
        it's genuinely ``degraded``, not ``blocked``.

    Adopting soft edges is what lets the graph model the real runtime
    dependency shape (e.g. dashboard transitively needs a lot of things)
    without lying by marking half the system blocked when one optional
    helper goes down.
    """

    target_id: str
    mode: Literal["all", "any"] = "all"
    hardness: Literal["hard", "soft"] = "hard"
    group: str | None = None


@dataclass
class ControlNode:
    """A single node in the control graph.

    Node IDs follow a ``kind:path`` convention:

        domain:journal
        subsystem:daily-notes
        component:obsidian
        req:obsidian/daily-note/log-section
        cap:task_create

    ``component_id`` on component-kind nodes matches the key used in
    ``work_buddy.health.components.COMPONENT_CATALOG`` — this is the
    existing tool/component identifier (they are 1:1 today).
    """

    id: str
    kind: NodeKind
    label: str
    description: str

    grouping_parents: list[str] = field(default_factory=list)
    dependencies: list[Edge] = field(default_factory=list)

    preference: Preference | None = None
    effective_state: EffectiveState = "unknown"

    component_id: str | None = None
    requirement_ids: list[str] = field(default_factory=list)
    affects_capabilities: list[str] = field(default_factory=list)

    status_reason: str = ""
    blocking_issues: list[str] = field(default_factory=list)
    primary_actions: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Plain-dict representation for JSON serialization."""
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "description": self.description,
            "grouping_parents": list(self.grouping_parents),
            "dependencies": [
                {
                    "target_id": e.target_id,
                    "mode": e.mode,
                    "hardness": e.hardness,
                    "group": e.group,
                }
                for e in self.dependencies
            ],
            "preference": self.preference,
            "effective_state": self.effective_state,
            "component_id": self.component_id,
            "requirement_ids": list(self.requirement_ids),
            "affects_capabilities": list(self.affects_capabilities),
            "status_reason": self.status_reason,
            "blocking_issues": list(self.blocking_issues),
            "primary_actions": list(self.primary_actions),
        }


@dataclass
class NodeCache:
    """Cache unit held by graph.py. Holds a full graph build and its timestamp."""

    nodes: dict[str, ControlNode]
    built_at: float
