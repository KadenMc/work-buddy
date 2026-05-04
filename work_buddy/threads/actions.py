"""Action Catalog — typed filtered lens over the capability + workflow registries.

DESIGN.md §10.2 is explicit: the Action Catalog
is **NOT a separate registry.** It is a filtered view over the existing
capability and workflow registries:

- ``is_action=True``  →  the entry is in the catalog at all.
- ``available_in`` matches the caller's :class:`InvocationContext` →
  the entry is visible to that caller.

DESIGN.md §10.3 + correction #14: ``wb_search`` and action inference
filter by ``available_in``. **The caller does NOT pass the
InvocationContext** — the gateway derives it from the calling
session's metadata. Tests + module-internal callers can pass an
explicit context for unit testing.

Stage 2 also defines the action-kind semantics (Standard /
Improvised / Suggestion). Stage 2.8 wires the ``decompose`` Standard
Action; this module ships:

- ``catalog_for(context, *, registry=None)`` — list ActionTemplates.
- ``find_action(name, *, context, registry=None)`` — single lookup
  with availability check.
- ``ActionTemplate`` — frozen view object combining
  capability/workflow data with v5 action fields.

The catalog is recomputed on every call (cheap — a filter walk over
~150 entries). Stage 2.x can add caching if profiling shows it's
needed; not premature.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional, Union

from work_buddy.threads.enums import InvocationContext


# ---------------------------------------------------------------------------
# ActionTemplate — frozen view object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionTemplate:
    """Filtered view of a registry entry exposed to action inference.

    Combines core registry fields with the v5 Stage 1.5 fields
    (is_action, available_in, intrinsic_amplifiers,
    parameter_schema_for_action, requires_post_review).
    """

    name: str
    description: str
    category: str
    parameters: dict[str, Any]
    available_in: frozenset[InvocationContext]
    intrinsic_amplifiers: dict[str, str]
    parameter_schema_for_action: dict[str, Any]
    requires_post_review: bool

    # Discriminator: 'capability' | 'workflow'
    kind: str

    # For workflows that originated as improvised actions promoted
    # to Standard, the originating Thread is recorded for provenance.
    improvised_origin_thread_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------


def _registry_entries(registry: Optional[dict] = None) -> Iterable:
    """Iterate registry entries.

    Caller passes ``registry`` for tests; default loads the live
    registry from ``work_buddy.mcp_server.registry.get_registry``.
    """
    if registry is None:
        # Lazy import to avoid registry-build cost at module load.
        from work_buddy.mcp_server.registry import get_registry
        registry = get_registry()
    return registry.values()


def _entry_to_template(entry: Any) -> Optional[ActionTemplate]:
    """Convert a Capability or WorkflowDefinition to an ActionTemplate.

    Returns None if the entry shape isn't recognized (defensive: the
    registry could in principle store other types in the future).
    """
    # Lazy imports to avoid cycles
    from work_buddy.mcp_server.registry import Capability, WorkflowDefinition

    if isinstance(entry, Capability):
        return ActionTemplate(
            name=entry.name,
            description=entry.description,
            category=entry.category,
            parameters=entry.parameters,
            available_in=frozenset(entry.available_in),
            intrinsic_amplifiers=dict(entry.intrinsic_amplifiers),
            parameter_schema_for_action=dict(entry.parameter_schema_for_action),
            requires_post_review=entry.requires_post_review,
            kind="capability",
        )
    if isinstance(entry, WorkflowDefinition):
        # WorkflowDefinition has .name; .description; no .category
        # by default — derive from the workflow's first segment if
        # absent (e.g. 'morning/morning-routine' → 'morning').
        return ActionTemplate(
            name=entry.name,
            description=entry.description,
            category=getattr(entry, "category", entry.name.split("/")[0]),
            parameters={},  # workflow params aren't structured the
                            # same way; parameter_schema_for_action
                            # is the canonical schema source.
            available_in=frozenset(entry.available_in),
            intrinsic_amplifiers=dict(entry.intrinsic_amplifiers),
            parameter_schema_for_action=dict(entry.parameter_schema_for_action),
            requires_post_review=entry.requires_post_review,
            kind="workflow",
            improvised_origin_thread_id=entry.improvised_origin_thread_id,
        )
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def catalog_for(
    context: InvocationContext,
    *,
    registry: Optional[dict] = None,
    include_categories: Optional[Iterable[str]] = None,
) -> list[ActionTemplate]:
    """Return all Action Catalog entries visible to the given context.

    Filters:
    - ``is_action == True``
    - ``context in entry.available_in``
    - optional: ``category in include_categories`` (None = all)

    The caller's context is NOT a search parameter the agent passes
    — it's derived server-side from session metadata. This function
    accepts it explicitly because it lives at the boundary between
    the gateway (which has the session) and the FSM (which needs
    a filtered view).
    """
    out: list[ActionTemplate] = []
    cats = set(include_categories) if include_categories is not None else None
    for entry in _registry_entries(registry):
        if not getattr(entry, "is_action", False):
            continue
        if context not in getattr(entry, "available_in", set()):
            continue
        if cats is not None and getattr(entry, "category", None) not in cats:
            continue
        tmpl = _entry_to_template(entry)
        if tmpl is not None:
            out.append(tmpl)
    return sorted(out, key=lambda t: (t.category, t.name))


def find_action(
    name: str,
    *,
    context: InvocationContext,
    registry: Optional[dict] = None,
) -> Optional[ActionTemplate]:
    """Look up a single action by name with availability check.

    Returns None if the action is not registered, not flagged as an
    action (``is_action=False``), or not available in ``context``.
    """
    for entry in _registry_entries(registry):
        if getattr(entry, "name", None) != name:
            continue
        if not getattr(entry, "is_action", False):
            return None
        if context not in getattr(entry, "available_in", set()):
            return None
        return _entry_to_template(entry)
    return None


def all_action_names(*, registry: Optional[dict] = None) -> list[str]:
    """Diagnostic helper: every entry name flagged ``is_action=True``,
    regardless of availability. Useful for telemetry / docs gen."""
    return sorted(
        e.name for e in _registry_entries(registry)
        if getattr(e, "is_action", False)
    )


def has_amplifier_above(
    template: ActionTemplate,
    dimension: str,
    threshold: str,
) -> bool:
    """True iff the action's intrinsic amplifier on ``dimension``
    exceeds the threshold. Used by the autonomy layer's
    ``pause_on_risk_amplifier`` check.

    Risk-rank order: low < medium < high. Comparison is done on the
    rank, not lexicographically.
    """
    rank = {"low": 0, "medium": 1, "high": 2}
    actual = template.intrinsic_amplifiers.get(dimension)
    if actual is None:
        return False
    if actual not in rank or threshold not in rank:
        return False
    return rank[actual] > rank[threshold]
