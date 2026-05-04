"""Universal action library — actions that apply to any thread.

Layered onto every per-source action library by the runner. The
backing implementations live in
:mod:`work_buddy.threads.universal_actions` and are registered as
capabilities in :mod:`work_buddy.mcp_server.registry` with
``is_action=True``.

The user can pick any of these from the action chip dropdown on a
group sub-thread (or, eventually, any sub-thread). The LLM
cluster-refinement step also sees them and may pick one as the
proposed action when no source-specific action fits (e.g., a cluster
of items that the system shouldn't act on but the user might want
deferred).
"""

from __future__ import annotations

from work_buddy.pipelines.actions import (
    CARDINALITY_PER_GROUP,
    ActionDescriptor,
    ActionLibrary,
)


# Ordered descriptor list — controls dropdown ordering in the UI.

UNIVERSAL_ACTIONS: list[ActionDescriptor] = [
    ActionDescriptor(
        capability_name="thread_dismiss",
        label="Dismiss",
        description=(
            "Mark this group sub-thread as dismissed. The group's items "
            "stay visible in the umbrella's audit log; nothing is acted "
            "on. Useful when the cluster is wrong or you've decided not "
            "to act on its items."
        ),
        cardinality=CARDINALITY_PER_GROUP,
        icon="x-circle",
    ),
    ActionDescriptor(
        capability_name="thread_defer",
        label="Defer",
        description=(
            "Resurface this group sub-thread later (default: 24 hours). "
            "Useful when you can't decide right now but don't want to "
            "lose the cluster."
        ),
        cardinality=CARDINALITY_PER_GROUP,
        default_params={"duration_hours": 24.0},
        icon="clock",
    ),
    ActionDescriptor(
        capability_name="thread_rename",
        label="Rename",
        description=(
            "Override the cluster label. The new title surfaces on the "
            "column header + everywhere this thread appears in the "
            "dashboard."
        ),
        cardinality=CARDINALITY_PER_GROUP,
        icon="edit",
    ),
]


UNIVERSAL_ACTION_LIBRARY = ActionLibrary(UNIVERSAL_ACTIONS)
"""The default universal action library merged into every source
pipeline's library by :func:`work_buddy.pipelines.runner.run_pipeline`."""
