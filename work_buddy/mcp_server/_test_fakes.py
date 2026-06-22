"""Auto_run callables used only by the conductor invariant test suite.

The conductor's auto_run subprocess restricts importable paths to
``work_buddy.*`` (see ``_execute_auto_run`` in ``conductor.py``), so test
fixtures that need an auto_run callable have to live inside the package.
This module exists for that purpose alone — production code paths should
not import it.
"""

from __future__ import annotations

from typing import Any


def fake_scan_changes() -> dict[str, Any]:
    """Return a deterministic dict large enough to exercise visibility paths.

    Used by ``tests.unit.test_conductor_response_invariants`` as the
    auto_run callable for its ``minimal_auto_run_workflow`` fixture.  The
    response should be substantial enough that any duplication into
    ``auto_ran[*].result`` would trip ``find_duplicated_subtrees`` at the
    default 200-char threshold.
    """
    return {
        "items": [f"item-{i}" for i in range(50)],
        "summary": "fifty items",
        "meta": {"total": 50, "kind": "fake"},
    }


def fake_scan_raises() -> dict[str, Any]:
    """Always raise — used to drive the conductor's failure response path.

    The subprocess runner converts the exception into a ``{"success":
    False, "error": ..., "traceback": ...}`` reply, which the conductor
    then routes to ``fail_task`` and, when no downstream step can run,
    to ``_build_blocked_response``.
    """
    raise RuntimeError("synthetic scan failure")
