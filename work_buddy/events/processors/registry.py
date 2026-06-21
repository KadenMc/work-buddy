"""Action registry: a source's declared ``action.name`` → the handler that runs it.

Each action carries its own consent spec. Most sinks are no-gate (the effect is
itself a surface the user sees and can act on); a *future* state-changing action
would set ``consent_action`` + ``consent_weight="high"`` to re-prompt per fire.
``notify`` is a pure notification sink — no gate. Only ``notify`` exists this
slice (``autonomy: notify_only``).

The set of registered names must stay in sync with `definition.KNOWN_ACTIONS`
(the validator's allow-list); `test_source_action` asserts they match.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from work_buddy.events.envelope import Event
from work_buddy.events.processors.notify import notify_action
from work_buddy.events.protocol import ProcessorResult, RunContext


@dataclass(frozen=True)
class Action:
    """A source action: its handler plus the consent it requires at fire time."""

    name: str
    run: Callable[[Event, object, RunContext], ProcessorResult]
    consent_action: str | None = None  # None => no gate (a pure sink)
    consent_weight: str = "low"


ACTIONS: dict[str, Action] = {
    "notify": Action(name="notify", run=notify_action, consent_action=None),
}


def get_action(name: str) -> Action | None:
    """Return the registered `Action` for ``name``, or None if unknown."""
    return ACTIONS.get(name)


def known_actions() -> set[str]:
    """The set of registered action names."""
    return set(ACTIONS)
