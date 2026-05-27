"""Summarization worker — drains the queue.

PRD §6 O2-O11. Runs on the existing 5-minute `conversation-observability-refresh`
cadence (piggybacked, no separate cron). One worker tick:

1. Read `cooldown_minutes` and `daily_budget_usd` from config.
2. Check today's spend against the budget; if exceeded, log + return.
3. Fetch eligible queue entries (cooldown-passed, FIFO).
4. For each entry (bounded per tick):
   - Resolve a `Summarizer` instance for the namespace.
   - Call `summarizer.refresh_one(item_id, force=True, ...)`.
   - On success: remove from queue.
   - On error: record_attempt (which `refresh_one_incremental` already
     does internally via `store.record_error`).

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

logger = logging.getLogger(__name__)


_DEFAULT_COOLDOWN_MINUTES = 30
_DEFAULT_DAILY_BUDGET_USD = 1.00
_DEFAULT_TICK_LIMIT = 20  # max items per tick (safety bound; queue catches up over many ticks)


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
        "cooldown_minutes": int(
            summ.get("cooldown_minutes", _DEFAULT_COOLDOWN_MINUTES)
        ),
        "daily_budget_usd": float(
            summ.get("daily_budget_usd", _DEFAULT_DAILY_BUDGET_USD)
        ),
        "tick_limit": int(summ.get("worker_tick_limit", _DEFAULT_TICK_LIMIT)),
    }


# ---------------------------------------------------------------------------
# Summarizer resolution per namespace
# ---------------------------------------------------------------------------


def _resolve_summarizer(namespace: str):
    """Return a configured Summarizer for the namespace, or None."""
    if namespace == "conversation_session":
        from work_buddy.conversation_observability.summarizer_binding import (
            get_session_summarizer,
        )
        return get_session_summarizer()
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
    limit: int | None = None,
) -> dict[str, Any]:
    """Drain up to `limit` eligible queue entries.

    Args:
        namespace: optional filter (default: drain all namespaces).
        bypass_cooldown: when True, ignore the per-session cooldown.
            Used by inline-trigger from consumers (PRD §6 O4) and by
            `force_recent=true` user commands.
        bypass_budget: when True, ignore the daily-budget circuit-breaker.
            Used by explicit user-triggered refresh.
        limit: max items to process this tick. Default from config.

    Returns a dict:
        - `processed`: count of successful refreshes
        - `skipped_cooldown`: count of items left in queue due to cooldown
        - `errored`: count of failures (errors are recorded on the items row)
        - `budget_paused`: True if the daily budget halted processing
        - `today_spend_usd`: current day's spend (after the tick)
        - `queue_depth_after`: remaining queue depth
    """
    from work_buddy.summarization import queue as queue_mod

    cfg = _resolve_config()
    cooldown = 0 if bypass_cooldown else cfg["cooldown_minutes"]
    tick_limit = limit if limit is not None else cfg["tick_limit"]

    # Daily-budget gate.
    spend = today_summarization_spend_usd()
    if not bypass_budget and spend >= cfg["daily_budget_usd"]:
        logger.info(
            "summarization worker: budget exhausted (spend=$%.4f >= $%.4f); pausing",
            spend, cfg["daily_budget_usd"],
        )
        return {
            "processed": 0,
            "skipped_cooldown": 0,
            "errored": 0,
            "budget_paused": True,
            "today_spend_usd": spend,
            "queue_depth_after": queue_mod.queue_depth(namespace),
        }

    # Pull eligible entries.
    eligible = queue_mod.dequeue_eligible(
        namespace=namespace,
        cooldown_minutes=cooldown,
        limit=tick_limit,
    )

    # Count cooldown-skipped (for visibility): how many ENTRIES exist for
    # the namespace minus how many we just picked up.
    total_queued = queue_mod.queue_depth(namespace)
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
            if node is None:
                # Likely an error or no content. record_attempt to track.
                queue_mod.record_attempt(ns, item_id, "refresh returned None")
                errored += 1
            else:
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
        except Exception as exc:
            queue_mod.record_attempt(ns, item_id, f"{type(exc).__name__}: {exc}")
            errored += 1
            logger.exception(
                "summarization worker: unexpected exception on %s/%s",
                ns, item_id,
            )

    return {
        "processed": processed,
        "skipped_cooldown": skipped_cooldown,
        "errored": errored,
        "budget_paused": False,
        "today_spend_usd": spend,
        "queue_depth_after": queue_mod.queue_depth(namespace),
    }
