"""Broker admission for in-process (local-device) embedding encodes.

All local sentence-transformer encoding — bulk index builds AND interactive
query/search encodes — runs on the one host GPU/CPU. This module funnels every
such encode through a SINGLE broker profile (``local:embedding``) so the broker
serializes them on that one device and an INTERACTIVE encode preempts a
BACKGROUND index rebuild *between its batches*.

Contrast with ``work_buddy.embedding.providers.lmstudio``, which uses a per-peer
profile (``lmstudio:<model_id>``): a remote LM-Link peer is its own device, so it
gets its own admission queue. The local GPU is one device → one profile.

Why this exists: without it, the default (no-offload) configuration has **no**
admission control between a cold rebuild and a live query — they contend on the
GPU with nothing yielding, so an interactive search stalls behind a background
rebuild. Wrapping encode work PER BATCH (never per whole build) is what lets a
long rebuild actually yield between batches.
"""
from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

from work_buddy.inference.broker import (
    Priority,
    QueueFull,
    QueueWaitTimeout,
    get_broker,
)
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

# One profile for ALL local-device embedding encode. Query (INTERACTIVE) and
# bulk-document (BACKGROUND) encode share it so the broker can let a query
# preempt a rebuild on the single shared GPU.
LOCAL_EMBED_PROFILE = "local:embedding"


@contextlib.contextmanager
def local_embed_slot(priority: Priority = Priority.BACKGROUND) -> Iterator[Any]:
    """Admit one in-process embedding encode under the broker — best-effort.

    Yields the broker ticket on admission, or ``None`` when admission is
    skipped. Admission is **best-effort by design**: if the broker is
    unreachable, or backpressure (``QueueWaitTimeout`` / ``QueueFull``) would
    otherwise abort, the encode still proceeds without a slot — strictly no
    worse than the pre-broker behavior, in which there was no admission at all.
    The job here is to ADD yielding where there was none, never to BLOCK the
    default encode path.

    Usage (wrap the smallest unit of encode work, e.g. one batch)::

        with local_embed_slot(Priority.BACKGROUND):
            vecs = model.encode(batch, ...)
    """
    try:
        slot_cm = get_broker().slot(profile=LOCAL_EMBED_PROFILE, priority=priority)
    except Exception as exc:  # broker / config unavailable (bare CLI or test rig)
        logger.debug(
            "Inference broker unavailable (%s); encoding without admission.", exc,
        )
        yield None
        return

    try:
        with slot_cm as ticket:
            yield ticket
    except (QueueWaitTimeout, QueueFull) as exc:
        # Backpressure on admission must never break the default encode path.
        # (model.encode does not raise broker errors, so this only ever fires
        #  for an admission timeout/full on slot acquisition, not the body.)
        logger.warning(
            "Local embed admission skipped (%s) on profile %r; encoding without a slot.",
            exc.kind, LOCAL_EMBED_PROFILE,
        )
        yield None
