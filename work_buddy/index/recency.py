"""Recency bias for the consolidated index — epoch-based, operates on ``Hit`` objects.

Adapted from ``ir/recency.py``. The IR version read ISO strings from
``metadata.start_time``; the consolidated ``Document`` carries an epoch ``timestamp``
field, so this version takes epoch seconds directly via a ``{doc_id: ts_epoch}`` map.
This is also what *fixes vault's dead recency* (the vault hydrator never emitted a
timestamp, so its recency was a silent no-op) — once a partition populates
``Document.timestamp``, recency works for it. Pure Python + math.
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from work_buddy.index.model import Hit

_LN2 = math.log(2)


def recency_weight(
    ts_epoch: float | None,
    *,
    half_life_days: float = 14.0,
    floor: float = 0.15,
    now_epoch: float | None = None,
) -> float:
    """A ``[floor, 1.0]`` exponential-decay multiplier for an epoch timestamp.

    ``weight = floor + (1 - floor) * exp(-ln2 * days_old / half_life)``.
    Returns ``1.0`` (no penalty) when ``ts_epoch is None`` or ``half_life_days <= 0``.
    """
    if ts_epoch is None or half_life_days <= 0:
        return 1.0
    if now_epoch is None:
        now_epoch = time.time()
    days_old = max((now_epoch - float(ts_epoch)) / 86400.0, 0.0)
    decay = math.exp(-_LN2 * days_old / half_life_days)
    return floor + (1.0 - floor) * decay


def apply_recency_bias(
    hits: "list[Hit]",
    timestamps: dict[str, float | None],
    *,
    half_life_days: float = 14.0,
    floor: float = 0.15,
    now_epoch: float | None = None,
) -> "list[Hit]":
    """Multiply each hit's score by its recency weight and re-sort, in place.

    ``timestamps`` maps ``doc_id → epoch seconds`` (or None). Records the multiplier
    and the pre-bias score on ``hit.signals`` (``recency_weight`` / ``raw_score``).
    """
    if not hits or half_life_days <= 0:
        return hits
    if now_epoch is None:
        now_epoch = time.time()
    for h in hits:
        w = recency_weight(
            timestamps.get(h.doc_id),
            half_life_days=half_life_days, floor=floor, now_epoch=now_epoch,
        )
        h.signals.setdefault("raw_score", h.score)
        h.signals["recency_weight"] = round(w, 4)
        h.score = round(h.score * w, 4)
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits
