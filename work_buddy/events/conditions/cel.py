"""CEL conditions — a safe, non-Turing-complete predicate over a delivered event
and its predecessor.

Compiled once at construction (malformed syntax raises, so the loader/validator
can reject a bad source before it ever runs); evaluated **fail-closed** — any
runtime error yields ``False`` so a watcher never fires on an inconclusive or
malformed condition (the same posture as `classify_evidence`).

The activation exposes the extracted value under both a nested and a flat name,
so an author can write either the object form or the scalar shorthand:

    event.data        the just-extracted value (scalar OR object)
    prev.data         the previously-extracted value
    current           shorthand for event.data
    event.type        the event type (e.g. ai.workbuddy.source.nvda.changed)
    event.source      the source URI (e.g. /wb/source/nvda)

So a scalar watch reads ``event.data != prev.data`` and an object watch reads
``event.data.price != prev.data.price``. (There is deliberately no bare ``prev``
scalar — it would collide with the ``prev`` map; use ``prev.data``.)

celpy ships only CEL's standard built-ins (no ``abs``/``math.*``); `_cel_functions`
registers a small curated, pure extra set so threshold conditions like
``abs(event.data.price - prev.data.price) / prev.data.price > 0.05`` read cleanly.
"""

from __future__ import annotations

from work_buddy.events.envelope import Event
from work_buddy.events.protocol import ConditionContext
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


def _cel_functions() -> dict:
    """Curated extras beyond CEL's built-ins — pure + deterministic. Extend
    deliberately (each addition widens the author-facing DSL surface)."""
    import celpy.celtypes as ct

    def _abs(x):
        return ct.DoubleType(abs(float(x)))

    return {"abs": _abs}


class CelCondition:
    """A compiled CEL predicate. ``evaluate`` always returns a plain ``bool``."""

    def __init__(self, expr: str) -> None:
        import celpy

        self.expr = expr
        self._env = celpy.Environment()
        ast = self._env.compile(expr)  # raises CELParseError on bad syntax
        self._program = self._env.program(ast, functions=_cel_functions())

    def evaluate(self, event: Event, prev: Event | None, ctx: ConditionContext) -> bool:
        import celpy

        data = event.data or {}
        current = data.get("current")
        prev_value = data.get("prev")
        activation = celpy.json_to_cel(
            {
                "event": {"data": current, "type": event.type, "source": event.source},
                "prev": {"data": prev_value},
                "current": current,
            }
        )
        try:
            result = self._program.evaluate(activation)
        except Exception as exc:  # noqa: BLE001 — fail-closed: never fire on error
            logger.debug("CEL %r eval raised (%s) — treating as False", self.expr, exc)
            return False
        # celpy may *return* a CELEvalError object rather than raise it.
        if type(result).__name__ == "CELEvalError":
            logger.debug("CEL %r returned an eval-error — treating as False", self.expr)
            return False
        return bool(result)
