"""Fail-closed authority guard for frozen legacy task writers.

Authority routing happens before a legacy callable is imported and invoked.  A
cutover can commit between that routing decision and the callable boundary, so
every frozen task mutation/sync entry point must independently re-read the
durable authority ledger before it can touch Markdown.
"""

from __future__ import annotations

from functools import wraps
from typing import Callable, ParamSpec, TypeVar, cast


_P = ParamSpec("_P")
_R = TypeVar("_R")


def assert_frozen_task_mutation_allowed() -> None:
    """Return only when verified legacy authority still permits task writes."""

    from work_buddy.tasks.runtime import native_task_mutation_authority

    if native_task_mutation_authority():
        from work_buddy.tasks.errors import TaskLegacyEffectRetired

        raise TaskLegacyEffectRetired()


def frozen_task_mutation_boundary(
    function: Callable[_P, _R],
) -> Callable[_P, _R]:
    """Re-check authority when a frozen legacy writer actually begins."""

    @wraps(function)
    def guarded(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        assert_frozen_task_mutation_allowed()
        return function(*args, **kwargs)

    return cast(Callable[_P, _R], guarded)


__all__ = [
    "assert_frozen_task_mutation_allowed",
    "frozen_task_mutation_boundary",
]
