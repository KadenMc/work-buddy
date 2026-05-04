"""``_kickoff_inference`` — fire PROPOSED → AWAITING_INFERENCE on a
freshly-spawned Thread.

Sub-thread spawn paths (``decompose_thread``, ``group_thread``, the
unified pipeline runner) all need this same nudge: a Thread that
sits in PROPOSED forever doesn't get enqueued by the inference
worker, so the agent never proposes intent / context / actions for
it. ``engine.transition(... TRIG_BEGIN_INFERENCE)`` does the nudge.

This module is the single home for that helper after the removal
of the legacy ``threads/source_pipelines.py``. Same body as before;
just extracted so the new ``pipelines/`` package and the existing
``decompose.py`` / ``group.py`` can import it without depending on
the deleted module.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def kickoff_inference(thread_id: str) -> None:
    """Fire PROPOSED → AWAITING_INFERENCE for ``thread_id``.

    Non-fatal: logs on failure (the thread is already persisted;
    the user can manually trigger inference via the dashboard if
    the kickoff missed).
    """
    try:
        from work_buddy.threads import engine
        from work_buddy.threads.fsm import TRIG_BEGIN_INFERENCE
        engine.transition(
            thread_id, TRIG_BEGIN_INFERENCE,
            actor="inciting",
            fire_side_effects=True,
        )
    except Exception as e:
        logger.warning(
            "kickoff_inference for %s failed: %s — thread will sit "
            "in PROPOSED until manually advanced",
            thread_id, e,
        )


# Back-compat alias so existing imports of the leading-underscore
# private form keep resolving until everything's migrated.
_kickoff_inference = kickoff_inference
