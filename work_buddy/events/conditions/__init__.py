"""Event conditions — predicates over a delivered event and its predecessor.

A `Condition` (see `events/protocol.py`) decides whether a source's action should
fire. `CelCondition` is the safe, non-Turing-complete default; later tiers (a
semantic-LLM gate) sit behind the same port.
"""

from work_buddy.events.conditions.cel import CelCondition

__all__ = ["CelCondition"]
