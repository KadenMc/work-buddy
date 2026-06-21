"""Action processors (sinks) for event sources.

An action is the effect a source fires when its condition passes — this slice
ships only `notify` (a pure notification sink). The `registry` maps a source's
declared `action.name` to its handler + consent spec.
"""

from work_buddy.events.processors.registry import Action, get_action, known_actions

__all__ = ["Action", "get_action", "known_actions"]
