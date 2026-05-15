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
      - **soft**: if the target is down, *the node itself keeps running
        but certain features it provides are reduced or entirely
        unavailable*. Failure cascades as ``degraded`` at worst; if the
        target is merely ``disabled``, nothing propagates (user made an
        explicit choice not to use it).

    ``fallback_note`` describes — in one human-readable sentence — what
    specifically happens when a soft dep is down. It is **the distinction
    between "works with a less-good fallback" and "the feature just goes
    away"**, and is surfaced in the Settings UI so users understand what
    they actually lose. Examples:

      - "Hybrid search on tasks falls back to substring matching" —
        graceful degradation; feature still works.
      - "Chat search is unavailable" — hard failure of this specific
        feature, but the rest of the dashboard continues.

    When populated, the UI shows the note as the tooltip on the soft-dep
    chip and prepends it to the "Operating without: X" status reason.
    Without a note, the UI falls back to a generic "may be reduced"
    message.
    """

    target_id: str
    mode: Literal["all", "any"] = "all"
    hardness: Literal["hard", "soft"] = "hard"
    group: str | None = None
    fallback_note: str | None = None


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
    # For component-kind nodes: the managed sidecar service name
    # (``svc.name``), when the component maps to one. Lets the dashboard
    # join sidecar event-log entries (``event.source``) to this component
    # for the per-component event chip. ``None`` when the component has no
    # sidecar service (most components — only ~4 do).
    sidecar_service: str | None = None
    requirement_ids: list[str] = field(default_factory=list)
    affects_capabilities: list[str] = field(default_factory=list)

    status_reason: str = ""
    blocking_issues: list[str] = field(default_factory=list)
    primary_actions: list[dict] = field(default_factory=list)

    # Fix system metadata (only meaningful for kind="requirement"):
    # mirror of the underlying RequirementDef so the UI can decide which
    # buttons to render without making a separate fetch per requirement.
    # Empty/None for nodes that don't have a fix concept (domains, etc.).
    fix_kind: str = "none"        # "none" | "programmatic" | "input_required" | "agent_handoff"
    fix_params: dict = field(default_factory=dict)
    fix_preview: str | None = None

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
                    "fallback_note": e.fallback_note,
                }
                for e in self.dependencies
            ],
            "preference": self.preference,
            "effective_state": self.effective_state,
            "component_id": self.component_id,
            "sidecar_service": self.sidecar_service,
            "requirement_ids": list(self.requirement_ids),
            "affects_capabilities": list(self.affects_capabilities),
            "status_reason": self.status_reason,
            "blocking_issues": list(self.blocking_issues),
            "primary_actions": list(self.primary_actions),
            "fix_kind": self.fix_kind,
            "fix_params": dict(self.fix_params),
            "fix_preview": self.fix_preview,
        }


@dataclass
class NodeCache:
    """Cache unit held by graph.py. Holds a full graph build and its timestamp."""

    nodes: dict[str, ControlNode]
    built_at: float
