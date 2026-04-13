"""Recency bias for IR search results.

Applies an exponential time-decay multiplier to relevance scores so that
recent documents are favored while older-but-uniquely-relevant documents
still surface.

Pure Python + math only — no heavy imports. Safe to import from the MCP
server process (avoids the sqlite3/asyncio deadlock; see registry.py).
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

_LN2 = math.log(2)


def recency_weight(
    timestamp_iso: str,
    *,
    half_life_days: float = 14.0,
    floor: float = 0.15,
    now: datetime | None = None,
) -> float:
    """Compute a [floor, 1.0] decay weight for a document timestamp.

    Uses exponential decay with a floor:
        weight = floor + (1 - floor) * exp(-ln(2) * days / half_life)

    Args:
        timestamp_iso: ISO 8601 timestamp string (from document metadata).
        half_life_days: Days until weight reaches the midpoint between 1.0
            and floor.  Default 14 (two weeks).
        floor: Minimum weight for very old documents.  Ensures that a
            uniquely-relevant old result isn't completely suppressed.
        now: Reference time (default: utcnow).  Exposed for testing.

    Returns:
        Weight in [floor, 1.0].  Returns 1.0 if timestamp can't be parsed.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    try:
        ts = datetime.fromisoformat(timestamp_iso)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return 1.0  # Can't parse → no penalty

    days_old = max((now - ts).total_seconds() / 86400, 0.0)

    if half_life_days <= 0:
        return 1.0  # Disabled

    decay = math.exp(-_LN2 * days_old / half_life_days)
    return floor + (1.0 - floor) * decay


def apply_recency_bias(
    results: list[dict[str, Any]],
    *,
    half_life_days: float = 14.0,
    floor: float = 0.15,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Apply recency decay to search results and re-sort.

    Multiplies each result's ``score`` by a time-decay weight derived from
    ``metadata.start_time`` (or ``metadata.end_time`` as fallback).  Results
    are re-sorted by the adjusted score.

    Each result dict gets two extra keys:
        - ``recency_weight``: the [floor, 1.0] multiplier that was applied
        - ``raw_score``: the original score before adjustment

    Args:
        results: List of result dicts (must have 'score' and 'metadata').
        half_life_days: Passed to :func:`recency_weight`.
        floor: Passed to :func:`recency_weight`.
        now: Reference time (default: utcnow).

    Returns:
        The same list, mutated in place and re-sorted descending by
        adjusted score.
    """
    if not results or half_life_days <= 0:
        return results

    if now is None:
        now = datetime.now(timezone.utc)

    for r in results:
        meta = r.get("metadata", {})
        ts = meta.get("start_time") or meta.get("end_time")

        w = recency_weight(
            ts, half_life_days=half_life_days, floor=floor, now=now,
        ) if ts else 1.0

        r["raw_score"] = r["score"]
        r["recency_weight"] = round(w, 4)
        r["score"] = round(r["score"] * w, 4)

    results.sort(key=lambda r: r["score"], reverse=True)
    return results
