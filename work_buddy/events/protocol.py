"""Event backbone protocols — Source / Processor / Condition (interfaces only).

This module defines the *contracts*; concrete Sources and Conditions are not
yet implemented. The shapes are intentionally minimal so later backends (native
capability / workflow, and eventually ``external_flow``) are implementations
behind these ports rather than a redesign.

A **Sink** is just a ``Processor`` whose effect is "land it in a thread /
notification / task" — there is no separate type.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from work_buddy.events.envelope import Event


@dataclass
class RunContext:
    """Correlation + provenance handed to a ``Processor`` — deliberately *not*
    the execution semantics of any backend (no n8n ``msg`` / Node-RED state)."""

    seq: int
    traceparent: str | None = None
    session: str | None = None
    budget_usd: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConditionContext:
    """Context for ``Condition.evaluate``. Minimal until conditions are built."""

    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProcessorManifest:
    """What a processor declares about itself — including what the policy gate
    rules on (``consent_action``) and at what weight."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    consent_action: str | None = None  # None => no gate (A5)
    consent_weight: str = "low"        # "low" | "high"


@dataclass
class ProcessorResult:
    """MCP-tool-shaped result of running a processor."""

    structured: dict[str, Any] | None = None
    text: str = ""
    is_error: bool = False


@runtime_checkable
class Source(Protocol):
    """Adapter that normalizes a delivery mechanism into ``Event``s."""

    name: str
    mode: str  # "push" | "pull"

    def activate(self, emit: Callable[[Event], None]) -> None:
        """Push sources: begin emitting via ``emit(event)``."""
        ...

    def deactivate(self) -> None: ...

    def fetch(self) -> list[Event]:
        """Pull sources: called on schedule; may return an empty list."""
        ...


@runtime_checkable
class Processor(Protocol):
    """The work a delivered event triggers (capability / workflow / sink)."""

    manifest: ProcessorManifest

    def run(self, event: Event, ctx: RunContext) -> ProcessorResult: ...


@runtime_checkable
class Condition(Protocol):
    """A predicate over an event + its predecessor (used once conditions exist)."""

    def evaluate(
        self, event: Event, prev: Event | None, ctx: ConditionContext
    ) -> bool: ...
