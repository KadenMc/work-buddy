"""Summarization worker — drains the queue.

PRD §6 O2-O11. Runs on the existing 5-minute `conversation-observability-refresh`
cadence (piggybacked, no separate cron). One worker tick:

1. Read `cooldown_minutes` and `daily_budget_usd` from config.
2. Check today's spend against the budget; if exceeded, log + return.
3. Fetch cooldown-eligible, non-dead-letter queue entries.
4. For each entry (bounded per tick):
   - Resolve a `Summarizer` instance for the namespace.
   - Call `summarizer.refresh_one(item_id, force=True, ...)`.
   - On success: remove from queue.
   - On error: classify, rotate, and consume an attempt only for
     item-intrinsic failures.

A single worker tick is bounded; if the queue is long, subsequent ticks
drain the rest. This is intentional — the global daily budget naturally
limits total per-day work.

Cooldown bypass is supported via a `bypass_cooldown=True` flag on the
tick function (used by inline-triggered refresh in P6).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from work_buddy.summarization.policy import summaries_active

logger = logging.getLogger(__name__)


_DEFAULT_COOLDOWN_MINUTES = 30
_DEFAULT_DAILY_BUDGET_USD = 1.00
_DEFAULT_TICK_LIMIT = 20  # max items per tick (safety bound; queue catches up over many ticks)
_DEFAULT_MAX_ATTEMPTS = 3


def _error_kind_value(value: Any) -> str:
    raw = getattr(value, "value", value)
    return raw if isinstance(raw, str) and raw else "unknown"


_ENVIRONMENTAL_ERROR_KINDS = frozenset({
    "backend_unavailable",
    "model_not_available",
    "timeout",
    "rate_limited",
    "auth",
})


# ---------------------------------------------------------------------------
# Daily-budget tracking via the existing cost log
# ---------------------------------------------------------------------------


def today_summarization_spend_usd() -> float:
    """Sum today's summarization-tagged cost log entries.

    Cost log entries with `trace_id` starting with `summarization.` are
    counted. Reads all session dirs under `data_dir('agents')` because the
    sidecar's own session may not be the only contributor.
    """
    import json
    from work_buddy.paths import data_dir

    today = datetime.now(timezone.utc).date()
    total = 0.0
    agents_root = data_dir("agents")
    if not agents_root.exists():
        return 0.0

    for sd in agents_root.iterdir():
        log = sd / "llm_costs.jsonl"
        if not log.exists():
            continue
        try:
            for line in log.read_text(encoding="utf-8").splitlines():
                try:
                    e = json.loads(line)
                except (ValueError, TypeError):
                    continue
                tid = e.get("trace_id", "") or ""
                if not tid.startswith("summarization."):
                    continue
                ts = e.get("timestamp", "")
                try:
                    dt = datetime.fromisoformat(ts)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    continue
                if dt.date() != today:
                    continue
                total += float(e.get("estimated_cost_usd", 0) or 0)
        except OSError:
            continue
    return round(total, 6)


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


def _resolve_config() -> dict[str, Any]:
    """Read worker config from `conversation_observability.summaries.*`."""
    try:
        from work_buddy.config import load_config

        cfg = load_config()
        summ = (cfg.get("conversation_observability") or {}).get("summaries", {}) or {}
    except Exception:
        summ = {}
    return {
        "active": summaries_active(),
        "cooldown_minutes": int(
            summ.get("cooldown_minutes", _DEFAULT_COOLDOWN_MINUTES)
        ),
        "daily_budget_usd": float(
            summ.get("daily_budget_usd", _DEFAULT_DAILY_BUDGET_USD)
        ),
        "tick_limit": int(summ.get("worker_tick_limit", _DEFAULT_TICK_LIMIT)),
        "max_attempts": int(summ.get("max_attempts", _DEFAULT_MAX_ATTEMPTS)),
    }


def summaries_enabled() -> bool:
    """Compatibility name for callers that need the activation policy."""
    return summaries_active()


# ---------------------------------------------------------------------------
# Summarizer resolution per namespace
# ---------------------------------------------------------------------------


def _resolve_summarizer(namespace: str):
    """Return a configured Summarizer for the namespace, or None.

    The worker ALWAYS builds a v2 (incremental) summarizer — its job is
    the v2 producer. The legacy singleton (`get_session_summarizer()`)
    stays at v1 for compatibility with v1-shape callers (tests, query
    helpers, the v1 cron). See PRD OQ19 + the binding's docstring.
    """
    if namespace == "conversation_session":
        from work_buddy.conversation_observability.summarizer_binding import (
            build_session_summarizer,
        )
        return build_session_summarizer(use_incremental=True)
    # Other namespaces don't go through the worker today.
    return None


# ---------------------------------------------------------------------------
# Public entry point: one worker tick
# ---------------------------------------------------------------------------


def run_worker_tick(
    *,
    namespace: str | None = None,
    bypass_cooldown: bool = False,
    bypass_budget: bool = False,
    bypass_inactive: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """Drain up to `limit` eligible queue entries.

    Args:
        namespace: optional filter (default: drain all namespaces).
        bypass_cooldown: when True, ignore the per-session cooldown.
            Used by inline-trigger from consumers and by
            `force_recent=true` user commands.
        bypass_budget: when True, ignore the daily-budget circuit-breaker.
            Used by explicit user-triggered refresh.
        bypass_inactive: when True, drain even though summaries are
            opted out or the backend pre-gate reports no plausible
            backend. For explicit one-off user requests only — routine
            callers (sidecar cron, inline triggers) must respect both
            gates. Doubles as the escape hatch should the plausibility
            check ever misjudge a working setup.
        limit: max items to process this tick. Default from config.

    Returns a dict:
        - `processed`: count of successful refreshes
        - `skipped_cooldown`: count of items left in queue due to cooldown
        - `errored`: count of failures (errors are recorded on the items row)
        - `dead_lettered`: rows at/over `max_attempts`, excluded from drainage
        - `budget_paused`: True if the daily budget halted processing
        - `opted_out` / `dormant` / `dormancy_reason`: activation gates
        - `today_spend_usd`: current day's spend (after the tick)
        - `queue_depth_after`: remaining ACTIVE queue depth (dead letters
          excluded; they're reported separately)
    """
    from work_buddy.summarization import queue as queue_mod
    from work_buddy.summarization.orchestrator import chain_has_plausible_backend
    from work_buddy.summarization.protocol import SummarizationError

    cfg = _resolve_config()
    cooldown = 0 if bypass_cooldown else cfg["cooldown_minutes"]
    tick_limit = limit if limit is not None else cfg["tick_limit"]
    max_attempts = int(cfg.get("max_attempts", _DEFAULT_MAX_ATTEMPTS))

    if not bypass_inactive and not cfg.get("active", True):
        stats = queue_mod.queue_stats(namespace, max_attempts=max_attempts)
        return {
            "processed": 0,
            "skipped_cooldown": 0,
            "errored": 0,
            "dead_lettered": stats["dead_lettered"],
            "budget_paused": False,
            "opted_out": True,
            "dormant": False,
            "dormancy_reason": None,
            "today_spend_usd": 0.0,
            "queue_depth_after": stats["active"],
        }

    if not bypass_inactive and not chain_has_plausible_backend():
        stats = queue_mod.queue_stats(namespace, max_attempts=max_attempts)
        return {
            "processed": 0,
            "skipped_cooldown": 0,
            "errored": 0,
            "dead_lettered": stats["dead_lettered"],
            "budget_paused": False,
            "opted_out": False,
            "dormant": True,
            "dormancy_reason": "no_backend",
            "today_spend_usd": 0.0,
            "queue_depth_after": stats["active"],
        }

    # Daily-budget gate.
    spend = today_summarization_spend_usd()
    if not bypass_budget and spend >= cfg["daily_budget_usd"]:
        logger.info(
            "summarization worker: budget exhausted (spend=$%.4f >= $%.4f); pausing",
            spend, cfg["daily_budget_usd"],
        )
        stats = queue_mod.queue_stats(namespace, max_attempts=max_attempts)
        return {
            "processed": 0,
            "skipped_cooldown": 0,
            "errored": 0,
            "dead_lettered": stats["dead_lettered"],
            "budget_paused": True,
            "opted_out": False,
            "dormant": False,
            "dormancy_reason": None,
            "today_spend_usd": spend,
            "queue_depth_after": stats["active"],
        }

    # Pull eligible entries.
    eligible = queue_mod.dequeue_eligible(
        namespace=namespace,
        cooldown_minutes=cooldown,
        limit=tick_limit,
        max_attempts=max_attempts,
    )

    # Count cooldown-skipped (for visibility): how many ENTRIES exist for
    # the namespace minus how many we just picked up.
    total_queued = queue_mod.queue_depth(
        namespace, include_dead_letters=False, max_attempts=max_attempts,
    )
    skipped_cooldown = max(0, total_queued - len(eligible))

    processed = 0
    errored = 0

    for entry in eligible:
        ns = entry["namespace"]
        item_id = entry["item_id"]

        summarizer = _resolve_summarizer(ns)
        if summarizer is None:
            logger.warning(
                "summarization worker: no summarizer for namespace %r; skipping %r",
                ns, item_id,
            )
            continue

        try:
            # Use refresh_one with force=True — the queue itself decided this
            # item is stale, so we don't want refresh_one to short-circuit
            # on its own staleness check.
            node = summarizer.refresh_one(item_id, force=True)
            # ``None`` is a clean no-content result.  Typed failures raise.
            queue_mod.remove(ns, item_id)
            processed += 1

            # Re-check budget after each call — bail mid-tick if exceeded.
            if not bypass_budget:
                spend = today_summarization_spend_usd()
                if spend >= cfg["daily_budget_usd"]:
                    logger.info(
                        "summarization worker: budget tripped mid-tick "
                        "(spend=$%.4f); pausing",
                        spend,
                    )
                    break
        except SummarizationError as exc:
            kind = _error_kind_value(exc.error_kind)
            queue_mod.record_failure(
                ns,
                item_id,
                str(exc),
                error_kind=kind,
                count_attempt=kind not in _ENVIRONMENTAL_ERROR_KINDS,
            )
            errored += 1
            logger.warning(
                "summarization worker: %s failure on %s/%s: %s",
                kind, ns, item_id, exc,
            )
        except Exception as exc:
            queue_mod.record_failure(
                ns,
                item_id,
                f"{type(exc).__name__}: {exc}",
                error_kind="unknown",
                count_attempt=True,
            )
            errored += 1
            logger.exception(
                "summarization worker: unexpected exception on %s/%s",
                ns, item_id,
            )

    stats = queue_mod.queue_stats(namespace, max_attempts=max_attempts)
    return {
        "processed": processed,
        "skipped_cooldown": skipped_cooldown,
        "errored": errored,
        "dead_lettered": stats["dead_lettered"],
        "budget_paused": False,
        "opted_out": False,
        "dormant": False,
        "dormancy_reason": None,
        "today_spend_usd": spend,
        "queue_depth_after": stats["active"],
    }


def enqueue_missing(
    *,
    namespace: str = "conversation_session",
    days: int = 3650,
) -> dict[str, Any]:
    """Reconcile missing/stale summaries into the queue without LLM calls."""
    from work_buddy.summarization import queue as queue_mod
    from work_buddy.summarization.protocol import DiscoveryWindow

    summarizer = _resolve_summarizer(namespace)
    if summarizer is None:
        return {
            "namespace": namespace,
            "candidates": 0,
            "enqueued": 0,
            "queue_depth": queue_mod.queue_depth(namespace),
        }

    candidates = summarizer.source.discover(DiscoveryWindow(days=days))
    stale = summarizer.store.select_stale(candidates)
    for item_id, _token in stale:
        queue_mod.enqueue(namespace, item_id)

    return {
        "namespace": namespace,
        "candidates": len(candidates),
        "enqueued": len(stale),
        "queue_depth": queue_mod.queue_depth(namespace),
    }
