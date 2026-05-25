"""ResilienceContext — the per-call object threaded through a guarded call.

Carries the operation key (telemetry correlation), the propagating
:class:`~work_buddy.resilience.deadline.Deadline`, a call identity (with an
optional parent link for nested calls), and a typed-key property bag.

The context is threaded explicitly as a parameter and *also* published in a
``ContextVar`` so synchronous code deep inside a callable — including code
running in a worker thread, since ``asyncio.to_thread`` snapshots the
context — can retrieve it without plumbing.

See ``.data/designs/resilience-framework/DESIGN.md`` §7–§8.
"""

from __future__ import annotations

import contextvars
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Generic, Iterator, TypeVar

from work_buddy.resilience.deadline import Deadline

V = TypeVar("V")


@dataclass(frozen=True)
class TypedKey(Generic[V]):
    """A type-safe key for the ResilienceContext property bag.

    Declare a module-level ``TypedKey`` constant rather than passing a bare
    string, so property access is namespace-safe and type-checked — never a
    ``dict[str, Any]``.
    """

    name: str


def _new_call_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class ResilienceContext:
    """Per-call state for one guarded call."""

    operation_key: str
    deadline: Deadline
    call_id: str = field(default_factory=_new_call_id)
    parent_call_id: str | None = None
    _properties: dict[str, Any] = field(default_factory=dict, repr=False)

    def get(self, key: TypedKey[V], default: V | None = None) -> V | None:
        return self._properties.get(key.name, default)

    def set(self, key: TypedKey[V], value: V) -> None:
        self._properties[key.name] = value

    def derive_child(
        self,
        *,
        operation_key: str | None = None,
        deadline: Deadline | None = None,
    ) -> "ResilienceContext":
        """A child context for a nested guarded call.

        New ``call_id``; ``parent_call_id`` links back to this context. The
        deadline propagates unchanged unless overridden (an inner call may
        only *tighten* it). Properties are shallow-copied so nested code
        reads inherited values without leaking its own writes upward.
        """
        return ResilienceContext(
            operation_key=operation_key or self.operation_key,
            deadline=deadline or self.deadline,
            parent_call_id=self.call_id,
            _properties=dict(self._properties),
        )


_CURRENT: contextvars.ContextVar["ResilienceContext | None"] = (
    contextvars.ContextVar("wb_resilience_context", default=None)
)


def current_context() -> "ResilienceContext | None":
    """The ResilienceContext for the running guarded call, or ``None``."""
    return _CURRENT.get()


@contextmanager
def use_context(ctx: "ResilienceContext") -> "Iterator[ResilienceContext]":
    """Bind ``ctx`` as the current context for the duration of the block."""
    token = _CURRENT.set(ctx)
    try:
        yield ctx
    finally:
        _CURRENT.reset(token)
