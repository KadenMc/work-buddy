"""Stage 2.9: bootstrap — wire all v5 state-entry handlers and
register the budget admission hook.

Sidecar startup calls :func:`bootstrap_v5` once at the start of
the process. Tests call it (or its constituent pieces) explicitly
in fixtures. Idempotent — safe to call multiple times in a
process; the FSM-engine handler list is additive but the
behaviour is convergent (publishing the same notification ID
replaces the existing card).

What gets wired
---------------

1. **AWAITING_INFERENCE** → enqueue inference into the LLM-call
   queue (``inference_worker.awaiting_inference_handler``).
2. **All wait states** (``awaiting_*_confirmation``,
   ``awaiting_*_clarification``, ``awaiting_confirmation``,
   ``awaiting_review``, ``awaiting_redirect``) → publish a
   Resolution Surface card via the notifications subsystem
   (``resolution_surface._state_entry_handler``).
3. **Terminal states** (DONE, DISMISSED, HANDED_OFF) → cascade
   any parent thread's MONITORING → DONE check
   (``decompose.cascade_handler``).
4. **LLM-call queue admission hook** → per-caller budget check
   (``budget.budget_admission_hook``). For Thread callers, the
   budget is read from the Thread's autonomy_policy.budget_usd
   automatically (no explicit set_caller_budget needed).

Tests check the wiring count by snapshotting the handler maps
before/after.
"""

from __future__ import annotations

import logging
from typing import Optional

from work_buddy.llm import budget, queue
from work_buddy.threads import (
    cleanup_adapters,
    cleanup_runner,
    decompose,
    engine,
    inference_worker,
    resolution_surface,
)

logger = logging.getLogger(__name__)


_BOOTSTRAPPED = False


def is_bootstrapped() -> bool:
    return _BOOTSTRAPPED


def bootstrap_v5(*, clear_first: bool = False) -> None:
    """Wire all v5 state-entry handlers + budget admission.

    Parameters
    ----------
    clear_first:
        If True, clears all previously-registered FSM state-entry
        handlers AND admission hooks before wiring. Useful for
        tests; production startup keeps the default False so a
        re-bootstrap (e.g., after a config reload) doesn't lose
        third-party-registered handlers.

    Side effects
    ------------
    - engine.register_state_entry_handler(...) for every state.
    - queue.register_admission_hook(budget.budget_admission_hook).
    """
    global _BOOTSTRAPPED

    if clear_first:
        engine.clear_state_entry_handlers()
        queue.clear_admission_hooks()
        _BOOTSTRAPPED = False

    # 1. AWAITING_INFERENCE → enqueue
    inference_worker.register_inference_dispatch_handler()

    # 2. Every wait state → publish Resolution Surface card
    resolution_surface.register_resolution_surface_handlers()

    # 3. Terminal states → cascade to parent
    decompose.register_cascade_handlers()

    # 4. LLM-call queue admission hook
    queue.register_admission_hook(budget.budget_admission_hook)

    # 5. CLEANING_UP state-entry handler (Stage 4.4)
    cleanup_runner.register_cleanup_runner()

    # 6. Default cleanup adapters (journal-note for Stage 4.4;
    #    chrome adapter lands in 4.13 alongside the pipeline).
    cleanup_adapters.register_default_adapters()

    _BOOTSTRAPPED = True
    logger.info("v5 bootstrap complete")


def teardown_v5() -> None:
    """Test-only: clear all state-entry handlers + admission hooks
    + cleanup adapters so the next test starts from a clean slate."""
    global _BOOTSTRAPPED
    engine.clear_state_entry_handlers()
    queue.clear_admission_hooks()
    from work_buddy.threads.cleanup import clear_cleanup_adapters
    clear_cleanup_adapters()
    _BOOTSTRAPPED = False
