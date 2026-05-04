"""Source-aware action library — the catalog of actions a pipeline's
group sub-threads can carry.

Each :class:`SourcePipeline` declares an :class:`ActionLibrary` listing
the capabilities that apply to its data source. The runner merges that
with universal actions (dismiss / defer / rename / approve-individually)
so every group sub-thread carries a baseline set, plus the source-
specific extensions.

The library is the canonical input to two surfaces:

1. :func:`work_buddy.pipelines.refine_clusters` — the LLM is given the
   library's per-group action descriptors and picks the single best
   one per cluster.
2. The dashboard action-chip dropdown — when the user clicks
   ``→ Tasks ▾`` on a column header, the dropdown options come from
   the library.

An :class:`ActionDescriptor` references an existing capability in the
work-buddy capability registry. Cardinality declares whether the
action is meant to act on one item, one group, or the whole umbrella.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


# Cardinality string constants (kept as constants rather than enums to
# stay JSON-friendly — these flow into the LLM prompt, the action chip
# UI, and the workflow JSON).
CARDINALITY_PER_ITEM = "per_item"
CARDINALITY_PER_GROUP = "per_group"
CARDINALITY_UMBRELLA = "umbrella"

VALID_CARDINALITIES = frozenset({
    CARDINALITY_PER_ITEM,
    CARDINALITY_PER_GROUP,
    CARDINALITY_UMBRELLA,
})


@dataclass(frozen=True)
class ActionDescriptor:
    """A single entry in an :class:`ActionLibrary`.

    Fields
    ------
    capability_name:
        Name of a registered capability in the capability registry.
        The capability MUST be marked ``is_action=True``. Resolved at
        dispatch time via the standard capability lookup.
    label:
        Short user-facing label shown in the action chip dropdown
        (e.g. ``"Close all tabs"``, ``"Route to tasks"``).
    description:
        Tooltip prose. Surfaces in the action picker + the LLM prompt.
    cardinality:
        One of :data:`CARDINALITY_PER_ITEM`, :data:`CARDINALITY_PER_GROUP`,
        :data:`CARDINALITY_UMBRELLA`. Determines how many threads the
        action applies to.
    default_params:
        Parameter template dict. The runtime fills in cluster-specific
        fields (``item_ids``, ``thread_id``, ...) at approval time;
        anything in ``default_params`` is merged in as-is.
    icon:
        Optional icon identifier for the dashboard chip (e.g. a Lucide
        icon name like ``"check-square"``). Frontend chooses a default
        if absent.
    """

    capability_name: str
    label: str
    description: str
    cardinality: str
    default_params: dict[str, Any] = field(default_factory=dict)
    icon: str | None = None

    def __post_init__(self) -> None:
        if self.cardinality not in VALID_CARDINALITIES:
            raise ValueError(
                f"Invalid cardinality {self.cardinality!r}; "
                f"must be one of {sorted(VALID_CARDINALITIES)}",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_name": self.capability_name,
            "label": self.label,
            "description": self.description,
            "cardinality": self.cardinality,
            "default_params": dict(self.default_params),
            "icon": self.icon,
        }


class ActionLibrary:
    """A collection of :class:`ActionDescriptor` entries with helpers
    for merging across sources and querying by cardinality.

    Construction is order-sensitive: when two libraries are merged, the
    second's descriptors override the first's for matching
    ``capability_name``. This is how a per-source library "wins" over
    universal defaults if it wants to customize an action's label /
    description for its domain.
    """

    def __init__(
        self,
        descriptors: Iterable[ActionDescriptor] = (),
    ) -> None:
        self._by_name: dict[str, ActionDescriptor] = {}
        for d in descriptors:
            self._by_name[d.capability_name] = d

    # ----------------------------------------------------------------
    # Construction helpers
    # ----------------------------------------------------------------

    def merged_with(self, other: ActionLibrary) -> ActionLibrary:
        """Return a new library combining ``self`` and ``other``.

        Entries from ``other`` win on ``capability_name`` collision.
        Used by the runner to layer per-source actions on top of
        universal actions.
        """
        combined: dict[str, ActionDescriptor] = dict(self._by_name)
        combined.update(other._by_name)
        return ActionLibrary(combined.values())

    def with_descriptor(self, d: ActionDescriptor) -> ActionLibrary:
        """Return a new library with one extra descriptor (or
        replacing an existing same-name entry)."""
        return self.merged_with(ActionLibrary([d]))

    # ----------------------------------------------------------------
    # Queries
    # ----------------------------------------------------------------

    def all(self) -> list[ActionDescriptor]:
        """All descriptors in registration order."""
        return list(self._by_name.values())

    def per_group_actions(self) -> list[ActionDescriptor]:
        """Descriptors with ``cardinality == per_group`` — the set
        offered to ``refine_clusters`` for proposal generation."""
        return [
            d for d in self._by_name.values()
            if d.cardinality == CARDINALITY_PER_GROUP
        ]

    def per_item_actions(self) -> list[ActionDescriptor]:
        return [
            d for d in self._by_name.values()
            if d.cardinality == CARDINALITY_PER_ITEM
        ]

    def umbrella_actions(self) -> list[ActionDescriptor]:
        return [
            d for d in self._by_name.values()
            if d.cardinality == CARDINALITY_UMBRELLA
        ]

    def by_name(self, capability_name: str) -> ActionDescriptor | None:
        """Look up a descriptor by its capability name; None if absent."""
        return self._by_name.get(capability_name)

    def has(self, capability_name: str) -> bool:
        return capability_name in self._by_name

    def __len__(self) -> int:
        return len(self._by_name)

    def __iter__(self):
        return iter(self._by_name.values())

    def to_list(self) -> list[dict[str, Any]]:
        """JSON-serialisable form. Used by the LLM prompt + dashboard
        action chip."""
        return [d.to_dict() for d in self._by_name.values()]
