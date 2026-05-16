"""Dashboard card registry.

A :class:`DashboardCard` is a panel-mounted widget that may be gated by
component-active state. The endpoint ``GET /api/dashboard/cards/<mount_point>``
evaluates every registered card's :data:`~work_buddy.control.gates.Gate`
against the live control graph and returns the cards that should mount.

The registry is the server half of the "feature card" pattern; the
client half is the JS renderer registered into ``window.wbCardRenderers``
by a matching frontend module. The card ``id`` string links the two.
A plugin extends the dashboard by calling :func:`register_card` and
shipping a renderer module — without editing any tab loader.

A component counts as active unless it is explicitly opted out — i.e.
its feature preference is ``unwanted``. Gating on the preference
directly (rather than the control graph's ``effective_state``) keeps
opt-out and opt-in symmetric and instant: the graph's component
``effective_state`` lags a re-enable until the next health reprobe,
which would leave a just-re-enabled card hidden for up to a minute.
This also matches the backend bridge gate in
``dashboard.api.get_system_state``. See ``architecture/feature-cards``.
"""

from __future__ import annotations

from dataclasses import dataclass

from work_buddy.control.gates import Component, Gate, evaluate, validate
from work_buddy.health.components import COMPONENT_CATALOG
from work_buddy.health.preferences import is_wanted


@dataclass(frozen=True)
class DashboardCard:
    """A registry entry describing one dashboard card.

    The render logic lives in JS (``window.wbCardRenderers[id]``); this
    descriptor carries only what the server needs to decide whether the
    card mounts and in what order.
    """

    id: str
    """Namespaced, stable id, e.g. ``"obsidian.bridge_sparkline"``. Also
    the key into ``window.wbCardRenderers`` on the client."""

    mount_point: str
    """Where the card mounts, e.g. ``"activity"``."""

    gate: Gate | None = None
    """Boolean expression over component-active state. ``None`` = always
    active."""

    mount_slot: int = 0
    """Render order within the mount point (ascending)."""

    needs_state_keys: tuple[str, ...] = ()
    """Which ``/api/state`` keys the renderer consumes. Documentation /
    diagnostics only today."""

    background_jobs: tuple[str, ...] = ()
    """Scheduled-job ids whose existence is justified by this card.
    Reserved for future scheduler-side gating; unused today."""


CARD_REGISTRY: dict[str, DashboardCard] = {}


def register_card(card: DashboardCard) -> None:
    """Register a card. Raises ``ValueError`` on duplicate id or on a
    gate that references a component not in ``COMPONENT_CATALOG``."""
    if card.id in CARD_REGISTRY:
        raise ValueError(f"card id already registered: {card.id!r}")
    validate(card.gate, set(COMPONENT_CATALOG.keys()))
    CARD_REGISTRY[card.id] = card


def active_component_ids() -> set[str]:
    """Component ids that are not explicitly opted out.

    A component is active unless its feature preference is ``unwanted``
    (``is_wanted`` returns ``False``). Undecided, wanted, required, and
    core components all count as active — only an explicit opt-out hides
    the cards that depend on it.
    """
    return {
        cid for cid in COMPONENT_CATALOG if is_wanted(cid) is not False
    }


def cards_for_tab(mount_point: str) -> list[dict]:
    """Active card descriptors for ``mount_point``, in ``mount_slot`` order.

    Evaluates each registered card's gate against the set of
    not-opted-out components.
    """
    active = active_component_ids()
    matches = [
        card
        for card in CARD_REGISTRY.values()
        if card.mount_point == mount_point and evaluate(card.gate, active)
    ]
    matches.sort(key=lambda c: c.mount_slot)
    return [{"id": c.id, "mount_slot": c.mount_slot} for c in matches]


# ---------------------------------------------------------------------------
# Built-in cards — the three Settings → Activity widgets.
#
# Renderers: work_buddy/dashboard/frontend/scripts/tabs/cards/*.py
# ---------------------------------------------------------------------------

register_card(
    DashboardCard(
        id="obsidian.bridge_sparkline",
        mount_point="activity",
        mount_slot=0,
        gate=Component("obsidian"),
        needs_state_keys=("bridge",),
    )
)
register_card(
    DashboardCard(
        id="core.event_log",
        mount_point="activity",
        mount_slot=1,
        needs_state_keys=("events",),
    )
)
register_card(
    DashboardCard(
        id="core.notification_log",
        mount_point="activity",
        mount_slot=2,
    )
)
