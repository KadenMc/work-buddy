"""Per-caller budget enforcement for the LLM-call queue.

Stage 1.8 deliverable: hook + cost-tally helper. Stage 2 wires the
hook into the queue at process start and configures per-Thread
budgets from the autonomy policy.

The split is deliberate: ``queue.py`` provides the admission-hook
mechanism (general infrastructure); this module provides the
*concrete* hook for budget enforcement (one client, optional). Other
clients (scheduled jobs, agents) can register their own admission
hooks without touching this module.

Cost source
-----------

For Thread callers (``caller_kind='thread'``), cumulative cost is
computed by summing ``data_json.cost_usd`` over the Thread's
``thread_events`` rows where ``kind`` is an ``*_inferred`` event. For
other callers, a generic cost-source callable can be registered.

DESIGN.md §9.4 (per-Thread budget enforcement) is the spec.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Callable, Optional

from work_buddy.llm.queue import AdmissionDecision

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-caller budget store
# ---------------------------------------------------------------------------
#
# Stage 1: in-memory dict keyed by caller_id. Stage 2 reads from
# (a) the Thread's autonomy_policy.budget_usd for thread callers,
# (b) configurable per-caller defaults for other callers.
# ---------------------------------------------------------------------------


_BUDGETS_USD: dict[str, float] = {}


def set_caller_budget(caller_id: str, budget_usd: float) -> None:
    """Set (or override) the budget cap for one caller."""
    _BUDGETS_USD[caller_id] = float(budget_usd)


def get_caller_budget(caller_id: str) -> Optional[float]:
    """Resolve the budget cap for a caller.

    For Thread callers (``caller_id`` starts with ``thread:``),
    falls back to reading ``autonomy_policy.budget_usd`` from the
    Thread itself if no explicit budget has been set. This makes
    per-Thread budgets zero-config — every Thread automatically
    respects its policy.

    Explicit ``set_caller_budget`` calls take precedence (for
    tests + manual overrides).
    """
    explicit = _BUDGETS_USD.get(caller_id)
    if explicit is not None:
        return explicit
    if caller_id.startswith("thread:"):
        thread_id = caller_id[len("thread:"):]
        try:
            from work_buddy.threads.store import get_thread
            thread = get_thread(thread_id)
            if thread is not None:
                return thread.autonomy_policy.budget_usd
        except Exception:
            return None
    return None


def clear_caller_budgets() -> None:
    _BUDGETS_USD.clear()


# ---------------------------------------------------------------------------
# Cost source — pluggable so non-Thread callers can be supported later
# ---------------------------------------------------------------------------


CostFn = Callable[[str], float]


def _default_thread_cost_source(caller_id: str) -> float:
    """Sum cost across the Thread's inference events.

    Expects ``caller_id`` of the shape ``"thread:<thread_id>"``.
    Returns 0.0 if the prefix is not ``thread:`` (other callers
    must register their own cost source).
    """
    if not caller_id.startswith("thread:"):
        return 0.0
    thread_id = caller_id[len("thread:"):]

    try:
        from work_buddy.threads.store import get_connection
    except Exception:
        # Threads subsystem not loadable — best-effort: assume 0 cost
        return 0.0

    total = 0.0
    try:
        conn = get_connection()
        try:
            rows = conn.execute(
                """SELECT data_json FROM thread_events
                   WHERE thread_id = ?
                   AND kind LIKE '%_inferred'""",
                (thread_id,),
            ).fetchall()
            for row in rows:
                data = row["data_json"]
                if not data:
                    continue
                try:
                    payload = json.loads(data) if isinstance(data, str) else data
                except (TypeError, ValueError):
                    continue
                cost = payload.get("cost_usd")
                if isinstance(cost, (int, float)):
                    total += float(cost)
        finally:
            conn.close()
    except Exception as e:
        logger.warning("Thread cost-source query failed: %s", e)
    return total


_COST_SOURCES: dict[str, CostFn] = {
    "thread": _default_thread_cost_source,
}


def register_cost_source(caller_kind: str, fn: CostFn) -> None:
    """Plug in a cumulative-cost source for a caller_kind.

    The function takes a ``caller_id`` and returns the cumulative
    cost in USD already incurred by that caller. Used by the budget
    admission hook to compare against the configured cap.
    """
    _COST_SOURCES[caller_kind] = fn


def reset_cost_sources() -> None:
    """Test-only: restore the default cost sources.

    Tests that override the cost source for fault-injection (e.g.
    "what if cumulative=$99?") leak that override into the
    process-global state. Bootstrap teardown calls this so the
    next test starts from the canonical default.
    """
    _COST_SOURCES.clear()
    _COST_SOURCES["thread"] = _default_thread_cost_source


def cumulative_cost_for(caller_id: str, caller_kind: str) -> float:
    """Return cumulative LLM cost in USD for a caller. 0.0 if no
    cost source is registered for ``caller_kind``."""
    fn = _COST_SOURCES.get(caller_kind)
    if fn is None:
        return 0.0
    try:
        return float(fn(caller_id))
    except Exception as e:
        logger.warning(
            "Cost source for %s/%s raised %s; treating as 0",
            caller_kind, caller_id, e,
        )
        return 0.0


# ---------------------------------------------------------------------------
# The admission hook
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetCheckResult:
    """Detailed view of the budget check for logging/audit."""

    cumulative_usd: float
    estimated_usd: float
    budget_usd: Optional[float]
    would_exceed: bool


def check_budget(
    caller_id: str,
    caller_kind: str,
    estimated_cost_usd: float,
) -> BudgetCheckResult:
    """Compute (without enforcing) whether a request would exceed
    the caller's budget. Useful for the admission hook AND for
    diagnostics."""
    budget = get_caller_budget(caller_id)
    cumulative = cumulative_cost_for(caller_id, caller_kind)
    would_exceed = (
        budget is not None
        and (cumulative + estimated_cost_usd) > budget
    )
    return BudgetCheckResult(
        cumulative_usd=cumulative,
        estimated_usd=estimated_cost_usd,
        budget_usd=budget,
        would_exceed=would_exceed,
    )


def budget_admission_hook(
    *,
    caller_id: str,
    caller_kind: str,
    target: str,
    payload: dict,
    tier_hint: Optional[str],
    estimated_cost_usd: float,
) -> AdmissionDecision:
    """The admission hook itself.

    Register at process start (Stage 2)::

        from work_buddy.llm.queue import register_admission_hook
        from work_buddy.llm.budget import budget_admission_hook
        register_admission_hook(budget_admission_hook)

    Stage 1: not registered by default — this module just provides
    the building blocks. Stage 2 registers it as part of sidecar
    bootstrap.
    """
    result = check_budget(caller_id, caller_kind, estimated_cost_usd)
    if result.would_exceed:
        reason = (
            f"budget exceeded: cumulative ${result.cumulative_usd:.4f} "
            f"+ estimated ${result.estimated_usd:.4f} > "
            f"cap ${result.budget_usd:.4f}"
        )
        return AdmissionDecision(admit=False, reason=reason)
    return AdmissionDecision(admit=True)
